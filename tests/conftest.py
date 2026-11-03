"""Shared fixtures. Nothing here calls the API."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, time

import pytest

from triage.agents import AgentCall, PriorReview
from triage.config import RulesSettings
from triage.schemas import (
    CallUsage,
    Case,
    Category,
    ChallengeResult,
    Channel,
    ContactRole,
    Objection,
    ObjectionResponse,
    Priority,
    Queue,
    RevisionResult,
    TriageResult,
)

WINDOW = RulesSettings(after_hours_start=time(22, 0), after_hours_end=time(6, 0))


def make_case(body: str, *, channel: Channel = Channel.EMAIL, at: str = "2026-10-20 14:00", **kw) -> Case:
    return Case(
        case_id=kw.pop("case_id", "TB-0001"),
        channel=channel,
        received_at=datetime.fromisoformat(at),
        contact_role=kw.pop("contact_role", ContactRole.RESIDENT),
        body=body,
        **kw,
    )


def make_triage(**overrides) -> TriageResult:
    fields = dict(
        category=Category.THERMOSTAT_HVAC,
        priority=Priority.P3,
        queue=Queue.TIER1_DEVICES,
        escalation_flags=[],
        draft_first_response="Thanks for letting us know. We're looking into your thermostat now.",
        confidence=0.8,
        rationale="Single thermostat unresponsive; the resident can still use the wall controls.",
    )
    return TriageResult(**(fields | overrides))


AGREE = ChallengeResult(verdict="agree", objections=[], severity="none")


def disagree(field: str = "priority", suggested: str | None = "P2") -> ChallengeResult:
    return ChallengeResult(
        verdict="disagree",
        objections=[Objection(field=field, problem="The case says the heat is out in freezing weather.", suggested_value=suggested)],
        severity="high",
    )


def usage(stage: str) -> CallUsage:
    return CallUsage(stage=stage, model="fake", input_tokens=100, output_tokens=50, cost_usd=0.001, latency_s=0.1)


class ScriptedAgents:
    """Plays back canned outputs and records every call, so tests can assert on what each agent saw.

    Running out of scripted challenges raises StopIteration, so a loop that overruns its
    bound fails loudly instead of hanging.
    """

    def __init__(
        self,
        triage: TriageResult,
        challenges: Iterable[ChallengeResult] = (),
        revisions: Iterable[TriageResult] = (),
    ):
        self._triage = triage
        self._challenges: Iterator[ChallengeResult] = iter(challenges)
        self._revisions: Iterator[TriageResult] = iter(revisions)
        self.calls: list[tuple] = []

    def triage(self, case: Case) -> AgentCall[TriageResult]:
        self.calls.append(("triage", case))
        return AgentCall(self._triage, usage("triage"))

    def challenge(self, case: Case, proposal: TriageResult, prior: PriorReview | None = None) -> AgentCall[ChallengeResult]:
        self.calls.append(("challenge", proposal, prior))
        return AgentCall(next(self._challenges), usage("recheck" if prior else "challenge"))

    def revise(self, case: Case, proposal: TriageResult, challenge: ChallengeResult) -> AgentCall[RevisionResult]:
        self.calls.append(("revise", proposal, challenge))
        revised = next(self._revisions, proposal)
        responses = [ObjectionResponse(field=o.field, decision="accept", reason="Fair point.") for o in challenge.objections]
        return AgentCall(RevisionResult(responses=responses, revised=revised), usage("revise"))

    def count(self, kind: str) -> int:
        return sum(1 for call in self.calls if call[0] == kind)


@pytest.fixture
def window() -> RulesSettings:
    return WINDOW


@pytest.fixture(autouse=True)
def no_real_api_client(monkeypatch):
    """Any test that builds a real Anthropic client fails, so a green run proves zero API calls."""

    def refuse(*args, **kwargs):
        raise AssertionError("tests must inject a fake client; a real anthropic.Anthropic() was constructed")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("anthropic.Anthropic", refuse)
