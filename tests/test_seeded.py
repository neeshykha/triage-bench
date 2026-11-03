"""The seeded-error test, on synthetic decisions with a scripted challenger. No network, no API key."""

import pytest

from eval.seeded_errors import KINDS, Item, build_items, corrupt, run_items, score, summarize
from triage.agents import AgentCall
from triage.schemas import Category, ChallengeResult, EscalationFlag, Objection, Priority, Queue

from .conftest import AGREE, make_case, make_triage, usage


def labeled(case_id, **fields):
    from triage.schemas import GoldLabel, LabeledCase

    triage = make_triage(**fields)
    gold = GoldLabel(category=triage.category, priority=triage.priority, queue=triage.queue, escalation_flags=triage.escalation_flags)
    return LabeledCase(case=make_case("x", case_id=case_id), label=gold), triage


P1_LOCKOUT = dict(category=Category.ACCESS_LOCKS, priority=Priority.P1, queue=Queue.TIER1_ACCESS)
P3_HOWTO = dict(category=Category.THERMOSTAT_HVAC, priority=Priority.P3, queue=Queue.TIER1_DEVICES)
OUTAGE = dict(category=Category.ACCESS_LOCKS, priority=Priority.P1, queue=Queue.TIER2_ESCALATIONS,
              escalation_flags=[EscalationFlag.PROPERTY_WIDE_OUTAGE])


def test_each_break_changes_exactly_what_it_says():
    _, p1 = labeled("A", **P1_LOCKOUT)
    _, p3 = labeled("B", **P3_HOWTO)
    _, outage = labeled("C", **OUTAGE)

    under, field, right = corrupt("under_triage", p1)
    assert (under.priority, field, right) == (Priority.P3, "priority", "P1") and under.category == p1.category
    over, _, right = corrupt("over_triage", p3)
    assert over.priority == Priority.P1 and right == "P3"
    wrong_cat, field, right = corrupt("wrong_category", p1)
    assert wrong_cat.category == Category.BILLING and wrong_cat.queue == Queue.PM_SUCCESS and right == "Access & Locks"
    wrong_q, field, right = corrupt("wrong_queue", p1)
    assert wrong_q.queue != p1.queue and wrong_q.category == p1.category and right == "Tier1-Access"
    dropped, field, right = corrupt("dropped_flag", outage)
    assert dropped.escalation_flags == [] and dropped.queue == Queue.TIER1_ACCESS and right == "property_wide_outage"

    assert corrupt("under_triage", p3) is None and corrupt("over_triage", p1) is None and corrupt("dropped_flag", p1) is None


def pool(n=60):
    shapes = [P1_LOCKOUT, P3_HOWTO, OUTAGE, dict(category=Category.HUB_CONNECTIVITY, priority=Priority.P3, queue=Queue.TIER1_DEVICES)]
    return [labeled(f"NG-{i:03d}", **shapes[i % len(shapes)]) for i in range(n)]


def test_items_are_balanced_and_each_case_is_used_once():
    items = build_items(pool(), per_kind=4, seed=1)
    kinds = [i.kind for i in items]
    assert all(kinds.count(k) == 4 for k in KINDS) and kinds.count("clean") == 20
    assert len({i.case.case.case_id for i in items}) == len(items)
    assert build_items(pool(), per_kind=4, seed=1)[0].case.case.case_id == items[0].case.case.case_id  # seeded


class CatchesEverything:
    """Objects to the broken field with the right value; agrees with clean decisions."""

    def __init__(self, items):
        self.answers = {i.case.case.case_id: i for i in items}

    def challenge(self, case, proposal, prior=None):
        item = self.answers[case.case_id]
        if item.kind == "clean":
            return AgentCall(AGREE, usage("challenge"))
        objection = Objection(field=item.field, problem="wrong", suggested_value=item.correct)
        return AgentCall(ChallengeResult(verdict="disagree", objections=[objection], severity="high"), usage("challenge"))


class AgreesWithEverything:
    def challenge(self, case, proposal, prior=None):
        return AgentCall(AGREE, usage("challenge"))


@pytest.mark.parametrize("agents_for, caught, false_alarms", [(CatchesEverything, 20, 0), (lambda items: AgreesWithEverything(), 0, 0)])
def test_summary_counts_catches_and_false_alarms(agents_for, caught, false_alarms):
    items = build_items(pool(), per_kind=4, seed=1)
    rows, stopped = run_items(items, agents_for(items), max_usd=5, workers=2)
    s = summarize(rows)
    assert stopped is None and (s["broken"], s["clean"]) == (20, 20)
    assert (s["caught"], s["pinpointed"], s["fixed"], s["false_alarms"]) == (caught, caught, caught, false_alarms)


def test_a_draft_only_objection_is_not_a_catch_or_a_false_alarm():
    lc, p = labeled("A", **P1_LOCKOUT)
    item = Item(lc, p, "clean", None, None)
    s = score(item, "disagree", [{"field": "draft_first_response", "problem": "too long", "suggested_value": None}])
    assert s == {"flagged": False, "pinpointed": False, "fixed": False, "draft_objection": True}


def test_a_suggested_fix_counts_when_it_contains_the_right_value():
    lc, p = labeled("A", **P1_LOCKOUT)
    item = Item(lc, corrupt("under_triage", p)[0], "under_triage", "priority", "P1")
    assert score(item, "disagree", [{"field": "priority", "problem": "x", "suggested_value": "P1 (active lockout)"}])["fixed"]
    assert not score(item, "disagree", [{"field": "priority", "problem": "x", "suggested_value": "P2"}])["fixed"]


def test_a_dropped_outage_flag_is_spotted_through_the_queue_too():
    # Dropping the flag also moves the queue off Tier 2, so an objection to the queue has seen the break.
    from eval.seeded_errors import wrong_fields

    lc, p = labeled("A", **OUTAGE)
    broken = corrupt("dropped_flag", p)[0]
    item = Item(lc, broken, "dropped_flag", "escalation_flags", "property_wide_outage", wrong_fields(p, broken))
    assert item.wrong == {"queue": "Tier2-Escalations", "escalation_flags": "property_wide_outage"}
    s = score(item, "disagree", [{"field": "queue", "problem": "outage", "suggested_value": "Tier2-Escalations"}])
    assert s["pinpointed"] and s["fixed"]


def test_refuses_inside_a_claude_session_but_dry_run_is_free(monkeypatch, tmp_path):
    from eval import seeded_errors

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    assert seeded_errors.main(["--max-usd", "1", "--runs", str(tmp_path)]) == 2
    assert seeded_errors.main(["--max-usd", "1", "--runs", str(tmp_path), "--dry-run"]) == 0
