"""The runner, driven with scripted agents. No network, no API key."""

import json

import anthropic
import httpx2
import pytest

from triage.orchestrator import Pipeline
from triage.run import ROOT, load_cases, main, preflight, run_cases, summarize
from triage.schemas import Category, EscalationFlag, Priority, Queue

from .conftest import ScriptedAgents, make_triage

SMOKE = ROOT / "data" / "cases_smoke.jsonl"
A = Pipeline("A", rules=None, challenger=False)


def test_smoke_cases_parse_with_unique_ids():
    cases = load_cases(SMOKE)
    assert len(cases) == 10
    assert len({c.case.case_id for c in cases}) == 10
    assert {"injection", "non_english", "short_sms", "resolved_lockout"} <= {t for c in cases for t in c.hard_case_tags}


@pytest.mark.parametrize(
    "env, refused",
    [
        ({"CLAUDECODE": "1", "ANTHROPIC_API_KEY": "k"}, "inside a Claude Code session"),
        ({}, "ANTHROPIC_API_KEY"),
        ({"ANTHROPIC_API_KEY": "k"}, None),
    ],
)
def test_preflight(env, refused):
    reason = preflight(env)
    assert (reason is None) if refused is None else (refused in reason)


def test_main_refuses_inside_a_claude_session_before_building_a_client(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert main(["--limit", "1"]) == 2  # the conftest guard would fail the test if a client were built


def test_run_writes_one_line_per_case(tmp_path):
    out = tmp_path / "results.jsonl"
    outcome = run_cases(load_cases(SMOKE), ScriptedAgents(make_triage()), A, max_usd=5, out_file=out)
    assert len(outcome.results) == 10 and outcome.stopped_by is None
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["result"]["case_id"] for line in lines] == [f"SMK-{i:02d}" for i in range(1, 11)]


def test_spend_cap_stops_before_the_next_case():
    # Each scripted call costs $0.001, so the cap is reached after the third case.
    outcome = run_cases(load_cases(SMOKE), ScriptedAgents(make_triage()), A, max_usd=0.0025)
    assert len(outcome.results) == 3 and outcome.not_started == 7
    assert "spend cap" in outcome.stopped_by


class Unreachable(ScriptedAgents):
    def triage(self, case):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_api_error_ends_the_run_and_keeps_what_finished():
    outcome = run_cases(load_cases(SMOKE), Unreachable(make_triage()), A, max_usd=5)
    assert outcome.results == [] and outcome.not_started == 10
    assert outcome.stopped_by.startswith("APIConnectionError")


def test_summary_scores_against_the_file_labels():
    cases = load_cases(SMOKE)[:3]  # SMK-01 lockout P1, SMK-02 resolved lockout P3, SMK-03 active leak P1
    triage = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P1, queue=Queue.TIER1_ACCESS)
    outcome = run_cases(cases, ScriptedAgents(triage), A, max_usd=5)
    s = summarize(outcome.results)
    assert s["schema_valid"] == 3
    assert s["category_correct"] == 2  # SMK-03 is a leak, not a lock
    assert (s["p1_caught"], s["p1_total"]) == (2, 2)
    assert (s["p1_called_correctly"], s["p1_called"]) == (2, 3)  # SMK-02 over-triaged
    assert s["cost_usd"] == pytest.approx(0.003)


def test_challenger_stats_score_each_outcome():
    from triage.orchestrator import run_case

    from .conftest import AGREE, disagree

    case = load_cases(SMOKE)[0]  # labeled Access & Locks, P1
    right = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P1, queue=Queue.TIER1_ACCESS)
    wrong = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P3, queue=Queue.TIER1_ACCESS)
    C = Pipeline("C", rules=None, challenger=True)
    scripts = [
        ScriptedAgents(wrong, challenges=[disagree(suggested="P1"), AGREE], revisions=[right]),  # catch, fixed
        ScriptedAgents(right, challenges=[disagree(suggested="P3"), AGREE], revisions=[wrong]),  # false alarm, broke
        ScriptedAgents(wrong, challenges=[AGREE]),  # miss
        ScriptedAgents(right, challenges=[AGREE]),  # agreed with a right answer
    ]
    results = [(case, run_case(case.case, agents, C)) for agents in scripts]
    stats = summarize(results)["challenger"]
    assert stats == {
        "challenged": 4, "catch": 1, "false_alarm": 1, "miss": 1, "agreed_right": 1, "fixed": 1, "broke": 1,
        "draft_objections": 0, "draft_accepted": 0,
    }


def test_a_draft_objection_is_not_a_routing_false_alarm():
    # Regression: the first live D run (2026-10-26) counted 8 valid draft objections as false alarms.
    from triage.orchestrator import run_case

    from .conftest import AGREE, disagree

    case = load_cases(SMOKE)[0]
    right = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P1, queue=Queue.TIER1_ACCESS)
    agents = ScriptedAgents(right, challenges=[disagree(field="draft_first_response", suggested=None), AGREE])
    stats = summarize([(case, run_case(case.case, agents, Pipeline("C", rules=None, challenger=True)))])["challenger"]
    assert stats["false_alarm"] == 0 and stats["agreed_right"] == 1
    assert stats["draft_objections"] == 1 and stats["draft_accepted"] == 1


def test_rescore_rebuilds_the_summary_without_api_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")  # rescoring must work inside a session: it spends nothing
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outcome = run_cases(load_cases(SMOKE), ScriptedAgents(make_triage()), A, max_usd=5, out_file=run_dir / "results.jsonl")
    (run_dir / "summary.json").write_text(json.dumps({"pipeline": "A", "cases": 0}))
    assert main(["--rescore", str(run_dir)]) == 0
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["cases"] == len(outcome.results) == 10 and summary["pipeline"] == "A" and "rescored_at" in summary


def test_challenger_judged_on_what_it_saw_after_rules():
    from triage.orchestrator import run_case

    from .conftest import AGREE, WINDOW

    case = load_cases(SMOKE)[0]  # "door wont open" by SMS: R1 lifts a P3 to P1 before the challenger looks
    wrong = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P3, queue=Queue.TIER1_ACCESS)
    D = Pipeline("D", rules=WINDOW, challenger=True)
    stats = summarize([(case, run_case(case.case, ScriptedAgents(wrong, challenges=[AGREE]), D))])["challenger"]
    assert stats["agreed_right"] == 1 and stats["miss"] == 0


def test_injection_flag_is_counted():
    case = next(c for c in load_cases(SMOKE) if "injection" in c.hard_case_tags)
    flagged = make_triage(escalation_flags=[EscalationFlag.INJECTION_ATTEMPT])
    s = summarize(run_cases([case], ScriptedAgents(flagged), A, max_usd=5).results)
    assert (s["injection_flagged"], s["injection_total"]) == (1, 1)
