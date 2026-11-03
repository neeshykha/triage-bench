"""Data contracts for every stage of the pipeline.

Agents only ever receive a `Case`. Gold labels live on `LabeledCase`, which never
reaches an agent, so no model can see the answer it's being scored against.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Taxonomy (Aneesh owns these definitions; prompts/taxonomy.md explains them) ---


class Category(StrEnum):
    ACCESS_LOCKS = "Access & Locks"
    THERMOSTAT_HVAC = "Thermostat/HVAC Controls"
    HUB_CONNECTIVITY = "Hub & Connectivity"
    LEAK_SENSOR = "Leak/Sensor Alert"
    RESIDENT_APP = "Resident App & Account"
    MOVE_IN_OUT = "Move-in/Move-out Provisioning"
    PM_ADMIN = "Property Manager Admin"
    HARDWARE_RMA = "Hardware/RMA"
    BILLING = "Billing (property-level)"
    SPAM_OTHER = "Spam/Other"


class Priority(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# Taxonomy v0.6 (2026-11-03) folded P4 into P3: Aneesh's support desk doesn't use P4, so
# how-tos and general questions are P3. Older runs and labels still say P4; they read as P3.
RETIRED_PRIORITIES = {"P4": "P3"}


def fold_retired(value: Any) -> Any:
    return RETIRED_PRIORITIES.get(value, value) if isinstance(value, str) else value


def most_urgent(*priorities: Priority) -> Priority:
    """P1 beats P2 beats P3."""
    return min(priorities, key=lambda p: int(p.value[1:]))


class Queue(StrEnum):
    TIER1_ACCESS = "Tier1-Access"
    TIER1_DEVICES = "Tier1-Devices"
    TIER1_ACCOUNTS = "Tier1-Accounts"
    TIER2_ESCALATIONS = "Tier2-Escalations"
    PM_SUCCESS = "PM-Success"
    HARDWARE_RMA = "Hardware-RMA"
    HUMAN_REVIEW = "Human-Review"


class EscalationFlag(StrEnum):
    SAFETY = "safety"
    PROPERTY_WIDE_OUTAGE = "property_wide_outage"
    LEGAL_OR_THREAT = "legal_or_threat"
    INJECTION_ATTEMPT = "injection_attempt"


class Channel(StrEnum):
    EMAIL = "Email"
    SMS = "SMS"
    WEB = "Web"


class ContactRole(StrEnum):
    RESIDENT = "Resident"
    PROPERTY_MANAGER = "Property Manager"
    UNKNOWN = "Unknown"


# --- Input ---


class Case(BaseModel):
    """One inbound case, as the support desk receives it."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    channel: Channel
    received_at: datetime  # property-local time; the after-hours rule reads it
    contact_role: ContactRole = ContactRole.UNKNOWN
    property_id: str | None = None
    subject: str | None = None
    body: str


# --- Agent outputs ---


class TriageResult(BaseModel):
    category: Category
    priority: Priority
    queue: Queue
    escalation_flags: list[EscalationFlag]
    draft_first_response: str
    confidence: float = Field(ge=0, le=1)
    rationale: str

    _fold_priority = field_validator("priority", mode="before")(classmethod(lambda cls, v: fold_retired(v)))

    def for_review(self) -> dict[str, Any]:
        """What the challenger sees: the decision, never the reasoning or confidence behind it."""
        return self.model_dump(mode="json", exclude={"rationale", "confidence"})


ReviewableField = Literal["category", "priority", "queue", "escalation_flags", "draft_first_response"]


class Objection(BaseModel):
    field: ReviewableField
    problem: str
    suggested_value: str | None  # None when the fix isn't one value, e.g. "rewrite the draft"


class ChallengeResult(BaseModel):
    verdict: Literal["agree", "disagree"]
    objections: list[Objection]
    severity: Literal["none", "low", "medium", "high"]

    @model_validator(mode="after")
    def _verdict_matches_objections(self) -> ChallengeResult:
        if self.verdict == "agree" and self.objections:
            raise ValueError("an agreeing challenger can't raise objections")
        if self.verdict == "disagree" and not self.objections:
            raise ValueError("a disagreement needs at least one objection")
        return self


class ObjectionResponse(BaseModel):
    field: ReviewableField
    decision: Literal["accept", "rebut"]
    reason: str


class RevisionResult(BaseModel):
    responses: list[ObjectionResponse]
    revised: TriageResult


# --- Orchestrator records ---


class CallUsage(BaseModel):
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float
    latency_s: float


class RuleOverride(BaseModel):
    rule_id: str
    stage: str  # which agent output the rule corrected
    field: Literal["priority", "escalation_flags"]
    before: str
    after: str
    reason: str


class Route(StrEnum):
    NO_CHALLENGER = "no_challenger"
    AGREED = "agreed_first_pass"
    AGREED_AFTER_REVISION = "agreed_after_revision"
    UNRESOLVED = "unresolved_disagreement"  # sent to Human-Review
    AGENT_ERROR = "agent_error"  # sent to Human-Review


class TraceStep(BaseModel):
    stage: str
    data: dict[str, Any]


class PipelineResult(BaseModel):
    case_id: str
    pipeline: str
    route: Route
    final: TriageResult | None  # None only when an agent failed before producing anything
    revisions: int
    rule_overrides: list[RuleOverride]
    trace: list[TraceStep]
    usage: list[CallUsage]
    error: str | None = None

    @property
    def queue(self) -> Queue:
        return self.final.queue if self.final else Queue.HUMAN_REVIEW

    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)

    @property
    def latency_s(self) -> float:
        return sum(u.latency_s for u in self.usage)


# --- Labeled data (never passed to an agent) ---


class GoldLabel(BaseModel):
    category: Category
    priority: Priority
    queue: Queue
    escalation_flags: list[EscalationFlag] = Field(default_factory=list)

    _fold_priority = field_validator("priority", mode="before")(classmethod(lambda cls, v: fold_retired(v)))


class Adjudication(BaseModel):
    """Aneesh's second look at a gold case every pipeline missed, after seeing what they said and why."""

    verdict: Literal["mine", "system", "both"]  # his label stands / his label was a slip / either answer is right
    why: str = ""
    first_contact: GoldLabel  # the label the first eval scored against
    decided: str  # ISO date


class LabelReview(BaseModel):
    """What happened when Aneesh labeled a gold case: his blind pick, the generator's label, and his note."""

    spec_label: GoldLabel  # what the generator was asked to write
    blind_label: GoldLabel | None  # his label before seeing the generator's
    notes: str = ""
    adjudication: Adjudication | None = None


class LabeledCase(BaseModel):
    case: Case
    label: GoldLabel
    hard_case_tags: list[str] = Field(default_factory=list)
    label_source: Literal["spec", "human"] = "spec"  # "human" once Aneesh has reviewed it
    brief: dict[str, Any] | None = None  # the generator's brief, kept for analysis; never shown to an agent
    review: LabelReview | None = None
    also_accept: list[GoldLabel] = Field(default_factory=list)  # other answers that count as right ("both defensible")
