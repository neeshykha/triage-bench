import pytest
from pydantic import TypeAdapter, ValidationError

from triage.schemas import ChallengeResult, Objection, Priority, RevisionResult, TriageResult, most_urgent

from .conftest import make_triage


@pytest.mark.parametrize("model", [TriageResult, ChallengeResult, RevisionResult])
def test_output_schemas_convert_to_structured_output_json(model):
    # Same conversion client.messages.parse() runs before sending; this is the SDK's internal
    # helper, so update the import if the SDK moves it.
    from anthropic.lib._parse._transform import transform_schema

    schema = transform_schema(TypeAdapter(model).json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_triage_result_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        make_triage(confidence=1.2)


def test_triage_result_rejects_unknown_category():
    with pytest.raises(ValidationError):
        make_triage(category="Plumbing")


def test_for_review_hides_reasoning_and_confidence():
    shown = make_triage().for_review()
    assert "rationale" not in shown and "confidence" not in shown
    assert shown["priority"] == "P3"


def test_agree_with_objections_is_rejected():
    with pytest.raises(ValidationError, match="can't raise objections"):
        ChallengeResult(
            verdict="agree",
            objections=[Objection(field="priority", problem="x", suggested_value="P1")],
            severity="low",
        )


def test_disagree_without_objections_is_rejected():
    with pytest.raises(ValidationError, match="at least one objection"):
        ChallengeResult(verdict="disagree", objections=[], severity="high")


def test_most_urgent_orders_p1_first():
    assert most_urgent(Priority.P3, Priority.P1, Priority.P2) == Priority.P1
    assert most_urgent(Priority.P3) == Priority.P3


def test_retired_p4_reads_as_p3():
    # Taxonomy v0.6 folded P4 into P3; saved runs and labels from before still say P4.
    from triage.schemas import GoldLabel

    old = GoldLabel.model_validate({"category": "Spam/Other", "priority": "P4", "queue": "Human-Review"})
    assert old.priority == Priority.P3
    assert "P4" not in str(TriageResult.model_json_schema())  # the model can't answer P4
