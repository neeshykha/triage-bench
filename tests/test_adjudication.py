"""The adjudication page builder, on a scripted eval run. No network, no API key."""

import json
import re

import pytest

from data.build_adjudication import build, group_of, page_data
from eval.run_eval import GOLD, run_all
from triage.config import load_settings
from triage.run import load_cases
from triage.schemas import Category, Priority, Queue

from .conftest import AGREE, ScriptedAgents, make_triage

CASES = load_cases(GOLD)
GOLD_BY_ID = {lc.case.case_id: lc for lc in CASES}


@pytest.mark.parametrize(
    "mine, said, group",
    [
        (("Access & Locks", "P2"), ("Access & Locks", "P3"), "more_urgent"),
        (("Access & Locks", "P2"), ("Access & Locks", "P1"), "less_urgent"),
        (("Hardware/RMA", "P3"), ("Access & Locks", "P3"), "category"),
        (("Hardware/RMA", "P2"), ("Access & Locks", "P3"), "both"),
    ],
)
def test_each_disagreement_lands_in_one_group(mine, said, group):
    pair = lambda c, p: {"category": c, "priority": p}
    assert group_of(pair(*mine), pair(*said)) == group


def test_page_shows_only_cases_every_pipeline_missed(tmp_path):
    settings = load_settings()
    # A always says Thermostat P3; C says the same, so a case is missed by both unless its gold label is Thermostat P3.
    scripted = lambda spec: ScriptedAgents(make_triage(), challenges=[AGREE] * len(CASES))
    run_all(["A", "C"], CASES, tmp_path, 5.0, settings, scripted, lambda _: None)
    data = page_data(tmp_path, GOLD_BY_ID, {"NG-003": "a guess"})
    expected = sorted(
        cid for cid, lc in GOLD_BY_ID.items()
        if (lc.label.category, lc.label.priority) != (Category.THERMOSTAT_HVAC, Priority.P3)
    )
    assert [c["id"] for c in data["cases"]] == expected
    first = data["cases"][0]
    assert first["said"] == {"category": "Thermostat/HVAC Controls", "priority": "P3", "count": 2, "of": 2}
    assert data["pipelines"] == ["A", "C"]
    assert next(c for c in data["cases"] if c["id"] == "NG-003")["guess"] == "a guess"


def test_case_text_cannot_close_the_data_script():
    page = build({"cases": [{"body": "</script><b>hi</b>"}]})
    payload = re.search(r'<script type="application/json" id="tb-data">(.*?)</script>', page, re.S).group(1)
    assert json.loads(payload)["cases"][0]["body"] == "</script><b>hi</b>"


def _answers_case(cid="NG-003"):
    from triage.schemas import GoldLabel

    lc = GOLD_BY_ID[cid]  # as it was before any adjudication
    lc = lc.model_copy(update={"review": lc.review.model_copy(update={"adjudication": None}), "also_accept": []})
    return lc, GoldLabel(category=Category.HARDWARE_RMA, priority=Priority.P2, queue=Queue.HARDWARE_RMA)


def test_merge_records_first_contact_and_is_safe_to_rerun():
    from data.merge_adjudication import adjudicate

    lc, _ = _answers_case()
    system = {lc.case.case_id: {"verdict": "system", "category": "Hardware/RMA", "priority": "P4", "why": "slip"}}
    once, problems = adjudicate([lc], system, {}, "2026-11-03")
    assert problems == []
    fixed = once[0]
    assert (fixed.label.category, fixed.label.priority, fixed.label.queue) == (Category.HARDWARE_RMA, Priority.P3, Queue.HARDWARE_RMA)
    assert fixed.review.adjudication.first_contact == lc.label  # P4 answers fold to P3, and the old label is kept
    mine = {lc.case.case_id: {"verdict": "mine", "why": "changed my mind"}}
    back, _ = adjudicate(once, mine, {}, "2026-11-04")
    assert back[0].label == lc.label and back[0].review.adjudication.first_contact == lc.label


def test_both_defensible_counts_either_answer_as_right():
    from data.merge_adjudication import adjudicate
    from eval.run_eval import score
    from triage.schemas import PipelineResult, Route

    from .conftest import make_triage

    lc, _ = _answers_case()
    both = {lc.case.case_id: {"verdict": "both", "category": "Thermostat/HVAC Controls", "priority": "P3"}}
    merged, _ = adjudicate([lc], both, {}, "2026-11-03")
    said = make_triage()  # Thermostat P3, the alternate
    result = PipelineResult(case_id=lc.case.case_id, pipeline="A", final=said, route=Route.NO_CHALLENGER,
                            revisions=0, rule_overrides=[], trace=[], usage=[])
    assert merged[0].label == lc.label and len(merged[0].also_accept) == 1
    assert score([(merged[0], result)])["both"] == 1
    assert score([(lc, result)])["both"] == int((lc.label.category, lc.label.priority) == (said.category, said.priority))


def test_merge_reports_bad_answers_instead_of_guessing():
    from data.merge_adjudication import adjudicate

    lc, _ = _answers_case()
    _, problems = adjudicate([lc], {"NG-999": {"verdict": "mine"}, lc.case.case_id: {"verdict": "system", "category": "Plumbing", "priority": "P2"}}, {}, "x")
    assert any("NG-999" in p for p in problems) and any("bad corrected label" in p for p in problems)
