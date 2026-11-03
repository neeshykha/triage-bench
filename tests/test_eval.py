"""The gold-set eval runner, driven with scripted agents. No network, no API key."""

import pytest

from eval import run_eval
from eval.run_eval import GOLD, finished, render, report, run_all, score, wilson, with_gold
from triage.config import load_settings
from triage.run import load_cases, load_run
from triage.schemas import Category, Priority, Queue

from .conftest import AGREE, ScriptedAgents, make_triage

SETTINGS = load_settings()
CASES = load_cases(GOLD)
GOLD_BY_ID = {lc.case.case_id: lc for lc in CASES}


def scripted(spec):
    return ScriptedAgents(make_triage(), challenges=[AGREE] * len(CASES))


def test_wilson_ranges_are_honest_at_small_n():
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(30, 60)
    assert lo < 0.5 < hi and hi - lo > 0.2  # half of 60 right could be anywhere from about 38% to 62%
    lo, hi = wilson(60, 60)
    assert hi == pytest.approx(1.0) and 0.9 < lo < 1.0  # even a perfect score isn't proof of 100%


def test_runs_every_pipeline_over_every_gold_case(tmp_path):
    log = []
    stops = run_all(["A", "C"], CASES, tmp_path, 5.0, SETTINGS, scripted, log.append)
    assert stops == {}
    for name in ("A", "C"):
        assert finished(tmp_path / name) == set(GOLD_BY_ID)
    assert any(line.startswith("C done") for line in log)


def test_resume_runs_only_the_cases_left(tmp_path):
    first = run_all(["A"], CASES, tmp_path, 0.0105, SETTINGS, scripted, lambda _: None)  # $0.001 a case: stops at 11
    assert "spend cap" in first["A"] and len(finished(tmp_path / "A")) == 11
    built = []

    def counting(spec):
        built.append(agents := scripted(spec))
        return agents

    assert run_all(["A"], CASES, tmp_path, 5.0, SETTINGS, counting, lambda _: None) == {}
    assert built[0].count("triage") == len(CASES) - 11
    assert len(load_run(tmp_path / "A")) == len(CASES)  # no case run twice
    assert run_all(["A"], CASES, tmp_path, 5.0, SETTINGS, counting, lambda _: None) == {}  # nothing left
    assert len(built) == 1


def test_the_cap_is_shared_so_the_total_stays_under_it(tmp_path):
    stops = run_all(["A", "B", "E"], CASES, tmp_path, 0.03, SETTINGS, scripted, lambda _: None)
    assert set(stops) == {"A", "B", "E"}
    spent = sum(r.cost_usd for n in "ABE" for _, r in load_run(tmp_path / n))
    assert spent <= 0.03 + 3 * 0.001  # each pipeline may finish the one case that crosses its slice


def test_scoring_reads_todays_gold_label_not_the_saved_one(tmp_path):
    run_all(["A"], CASES, tmp_path, 5.0, SETTINGS, scripted, lambda _: None)
    saved = load_run(tmp_path / "A")
    case_id = saved[0][0].case.case_id
    # Scripted triage always says Thermostat P3; relabel one gold case to match and the score moves by one.
    relabeled = dict(GOLD_BY_ID)
    lc = relabeled[case_id]
    relabeled[case_id] = lc.model_copy(
        update={"label": lc.label.model_copy(update={"category": Category.THERMOSTAT_HVAC, "priority": Priority.P3,
                                                     "queue": Queue.TIER1_DEVICES})}
    )
    before, after = score(with_gold(saved, GOLD_BY_ID)), score(with_gold(saved, relabeled))
    was_right = GOLD_BY_ID[case_id].label.category == Category.THERMOSTAT_HVAC
    assert after["category"] == before["category"] + (0 if was_right else 1)


def test_report_writes_a_table_row_per_pipeline(tmp_path):
    run_all(["A", "C"], CASES, tmp_path, 5.0, SETTINGS, scripted, lambda _: None)
    text = report(tmp_path, ["A", "C"], SETTINGS, GOLD_BY_ID, out_dir=tmp_path)
    assert (tmp_path / "results.md").read_text() == text and (tmp_path / "results.json").exists()
    assert "| **A** " in text and "| **C** " in text
    assert "## The challenger" in text  # C was challenged; A wasn't, so only C gets a row there
    challenger_rows = text.split("## The challenger")[1].split("## Injection")[0]
    assert "| C |" in challenger_rows and "| A |" not in challenger_rows
    assert "## Missed by every pipeline" in text


def test_empty_inputs_score_and_render_without_crashing():
    s = score([])
    assert s["cases"] == 0 and s["p1_recall"] == [0, 0]
    assert "None." in render("x", {}, {}, GOLD_BY_ID)


def test_refuses_inside_a_claude_session(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert run_eval.main(["--pipelines", "A"]) == 2  # the conftest guard would fail the test if a client were built


def test_unknown_pipeline_is_refused():
    assert run_eval.main(["--pipelines", "Z"]) == 2


def test_first_pass_spread_counts_cases_where_the_same_agent_disagreed_with_itself(tmp_path):
    from eval.run_eval import first_pass_spread

    run_all(["A"], CASES, tmp_path / "x", 5.0, SETTINGS, scripted, lambda _: None)
    run_all(["A"], CASES, tmp_path / "y", 5.0, SETTINGS,
            lambda spec: ScriptedAgents(make_triage(priority=Priority.P2)), lambda _: None)
    same = {"A1": load_run(tmp_path / "x" / "A"), "A2": load_run(tmp_path / "x" / "A")}
    differ = {"A1": load_run(tmp_path / "x" / "A"), "A2": load_run(tmp_path / "y" / "A")}
    assert first_pass_spread(same) == 0
    assert first_pass_spread(differ) == len(CASES)
