"""The control loop. Plain Python, not a prompt: every path a case can take is in this file.

    case -> [rules pre-check] -> triage -> [rules] -> challenger --agree--> final
                                                          |
                                                       disagree
                                                          v
                         revise -> [rules] -> challenger re-check --agree--> final
                         (at most max_revisions rounds; still disagreeing after that -> Human-Review)

    Any agent failure (refusal, truncation, invalid output) -> Human-Review.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from .agents import AgentCall, AgentError, Agents, PriorReview
from .config import RulesSettings, Settings
from .rules import apply_rules, precheck
from .schemas import (
    Case,
    ChallengeResult,
    PipelineResult,
    Priority,
    Queue,
    Route,
    TraceStep,
    TriageResult,
    most_urgent,
)

logger = logging.getLogger(__name__)

PRIORITY_VALUES = {p.value for p in Priority}


@dataclass(frozen=True)
class Pipeline:
    """One configuration under test, e.g. "D" = triage + challenger + rules."""

    name: str
    rules: RulesSettings | None  # None switches the rules layer off
    challenger: bool
    max_revisions: int = 1

    @classmethod
    def from_settings(cls, name: str, settings: Settings) -> Pipeline:
        spec = settings.pipelines[name]
        return cls(
            name=name,
            rules=settings.rules if spec.rules else None,
            challenger=spec.challenger,
            max_revisions=settings.max_revisions,
        )


def escalate(proposal: TriageResult, challenge: ChallengeResult) -> TriageResult:
    """An unresolved disagreement goes to a person. Until one looks, the more urgent reading wins."""
    suggested = [
        Priority(o.suggested_value)
        for o in challenge.objections
        if o.field == "priority" and o.suggested_value in PRIORITY_VALUES
    ]
    return proposal.model_copy(
        update={"queue": Queue.HUMAN_REVIEW, "priority": most_urgent(proposal.priority, *suggested)}
    )


class _Run:
    """Collects the trace, usage, and rule overrides for one case as it moves through the loop."""

    def __init__(self, case: Case, pipeline: Pipeline):
        self.case = case
        self.pipeline = pipeline
        self.trace: list[TraceStep] = []
        self.usage = []
        self.overrides = []
        self.signals = precheck(case, pipeline.rules) if pipeline.rules else None
        if self.signals:
            self.trace.append(TraceStep(stage="precheck", data=asdict(self.signals)))

    def record(self, stage: str, call: AgentCall):
        self.usage.append(call.usage)
        self.trace.append(TraceStep(stage=stage, data=call.output.model_dump(mode="json")))
        return call.output

    def enforce(self, result: TriageResult, stage: str) -> TriageResult:
        if self.signals is None:
            return result
        result, overrides = apply_rules(self.signals, result, stage)
        self.overrides.extend(overrides)
        return result

    @property
    def revisions(self) -> int:
        return sum(1 for step in self.trace if step.stage == "revise")

    def finish(self, route: Route, final: TriageResult | None, error: str | None = None) -> PipelineResult:
        result = PipelineResult(
            case_id=self.case.case_id,
            pipeline=self.pipeline.name,
            route=route,
            final=final,
            revisions=self.revisions,
            rule_overrides=self.overrides,
            trace=self.trace,
            usage=self.usage,
            error=error,
        )
        logger.info("%s [%s] %s -> %s", result.case_id, result.pipeline, route.value, result.queue.value)
        return result


def run_case(case: Case, agents: Agents, pipeline: Pipeline) -> PipelineResult:
    run = _Run(case, pipeline)
    proposal: TriageResult | None = None
    try:
        proposal = run.enforce(run.record("triage", agents.triage(case)), "triage")
        if not pipeline.challenger:
            return run.finish(Route.NO_CHALLENGER, proposal)

        challenge = run.record("challenge", agents.challenge(case, proposal))
        # The range() is the whole bound: at most max_revisions revise calls, then a person decides.
        for _ in range(pipeline.max_revisions):
            if challenge.verdict == "agree":
                break
            revision = run.record("revise", agents.revise(case, proposal, challenge))
            proposal = run.enforce(revision.revised, "revise")
            challenge = run.record("recheck", agents.challenge(case, proposal, PriorReview(challenge, revision)))

        if challenge.verdict == "agree":
            return run.finish(Route.AGREED if run.revisions == 0 else Route.AGREED_AFTER_REVISION, proposal)
        return run.finish(Route.UNRESOLVED, escalate(proposal, challenge))

    except AgentError as err:
        if err.usage:
            run.usage.append(err.usage)
        # Keep whatever triage produced so a person starts from it; the queue is Human-Review either way.
        final = proposal.model_copy(update={"queue": Queue.HUMAN_REVIEW}) if proposal else None
        return run.finish(Route.AGENT_ERROR, final, error=str(err))
