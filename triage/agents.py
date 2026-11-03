"""Thin wrappers around the three Claude calls. No control flow lives here; that's orchestrator.py.

Each call returns structured output validated against a pydantic schema, plus a
`CallUsage` record with tokens, cost, and latency so the eval can price every config.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from .config import AgentSettings, Price, Settings
from .schemas import (
    CallUsage,
    Case,
    ChallengeResult,
    RevisionResult,
    TriageResult,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"

T = TypeVar("T", bound=BaseModel)


class AgentError(RuntimeError):
    """A call that came back unusable: a refusal, a truncation, or output that failed validation.

    Carries the call's usage when there is one, so a failed call still shows up in the cost.
    """

    def __init__(self, message: str, usage: CallUsage | None = None):
        super().__init__(message)
        self.usage = usage


@dataclass(frozen=True)
class AgentCall(Generic[T]):
    output: T
    usage: CallUsage


@dataclass(frozen=True)
class PriorReview:
    """The challenger's earlier objections and the triage agent's answers to them."""

    challenge: ChallengeResult
    revision: RevisionResult


class Agents(Protocol):
    """The three calls the orchestrator makes. Tests swap in a scripted fake."""

    def triage(self, case: Case) -> AgentCall[TriageResult]: ...

    def challenge(
        self, case: Case, proposal: TriageResult, prior: PriorReview | None = None
    ) -> AgentCall[ChallengeResult]: ...

    def revise(self, case: Case, proposal: TriageResult, challenge: ChallengeResult) -> AgentCall[RevisionResult]: ...


# --- Prompt assembly ---


def load_prompt(role: str) -> str:
    """The shared taxonomy followed by the role prompt, with authoring comments stripped."""
    text = (PROMPTS_DIR / "taxonomy.md").read_text() + "\n\n" + (PROMPTS_DIR / f"{role}.md").read_text()
    return re.sub(r"<!--.*?-->\s*", "", text, flags=re.DOTALL).strip()


def _escape(text: str) -> str:
    # Sender text can't open or close a tag, so it can't break out of the data block.
    return html.escape(text, quote=False)


def render_case(case: Case) -> str:
    return (
        f'<case id="{case.case_id}" channel="{case.channel}" contact_role="{case.contact_role}" '
        f'received="{case.received_at:%Y-%m-%d %H:%M} property local time">\n'
        f"<subject>{_escape(case.subject or '')}</subject>\n"
        f"<body>\n{_escape(case.body)}\n</body>\n"
        f"</case>"
    )


def _json_block(tag: str, model: BaseModel | dict) -> str:
    body = model if isinstance(model, dict) else model.model_dump(mode="json")
    return f"<{tag}>\n{json.dumps(body, indent=2)}\n</{tag}>"


def render_challenge_input(case: Case, proposal: TriageResult, prior: PriorReview | None) -> str:
    parts = [render_case(case), _json_block("triage_decision", proposal.for_review())]
    if prior:
        # Only the exchange about the challenger's own objections; never the original rationale.
        parts.append(
            "<previous_review>\n"
            + _json_block("your_objections", prior.challenge)
            + "\n"
            + _json_block("triage_responses", {"responses": [r.model_dump(mode="json") for r in prior.revision.responses]})
            + "\n</previous_review>"
        )
    return "\n\n".join(parts)


def render_revise_input(case: Case, proposal: TriageResult, challenge: ChallengeResult) -> str:
    return "\n\n".join([render_case(case), _json_block("your_triage", proposal), _json_block("review", challenge)])


def cost_usd(price: Price, usage: anthropic.types.Usage) -> float:
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + (usage.cache_creation_input_tokens or 0) * price.cache_write
        + (usage.cache_read_input_tokens or 0) * price.cache_read
    ) / 1_000_000


# --- The real thing ---


class ClaudeAgents:
    def __init__(
        self,
        settings: Settings,
        client: anthropic.Anthropic | None = None,
        triage_model: str | None = None,
        challenger_model: str | None = None,
    ):
        self._client = client or anthropic.Anthropic()
        self._prices = settings.prices
        triage = settings.agents["triage"]
        challenger = settings.agents["challenger"]
        self._triage = triage.model_copy(update={"model": triage_model}) if triage_model else triage
        self._challenger = challenger.model_copy(update={"model": challenger_model}) if challenger_model else challenger
        self._prompts = {role: load_prompt(role) for role in ("triage", "challenger", "revise")}

    def triage(self, case: Case) -> AgentCall[TriageResult]:
        return self._call("triage", self._triage, "triage", render_case(case), TriageResult)

    def challenge(
        self, case: Case, proposal: TriageResult, prior: PriorReview | None = None
    ) -> AgentCall[ChallengeResult]:
        stage = "recheck" if prior else "challenge"
        return self._call(stage, self._challenger, "challenger", render_challenge_input(case, proposal, prior), ChallengeResult)

    def revise(self, case: Case, proposal: TriageResult, challenge: ChallengeResult) -> AgentCall[RevisionResult]:
        return self._call("revise", self._triage, "revise", render_revise_input(case, proposal, challenge), RevisionResult)

    def _call(self, stage: str, agent: AgentSettings, role: str, user: str, output: type[T]) -> AgentCall[T]:
        # Refusal fallbacks are deliberately off: a silent switch to another model would
        # corrupt per-config cost and accuracy. A refusal is an AgentError, and the
        # orchestrator sends the case to Human-Review.
        start = time.perf_counter()
        try:
            response = self._client.messages.parse(
                model=agent.model,
                max_tokens=agent.max_tokens,
                system=[{"type": "text", "text": self._prompts[role], "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": agent.effort},
                output_format=output,
            )
        except ValidationError as err:
            raise AgentError(f"{stage}: output failed schema validation: {err}") from err
        usage = CallUsage(
            stage=stage,
            model=agent.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
            cost_usd=cost_usd(self._prices[agent.model], response.usage),
            latency_s=time.perf_counter() - start,
        )
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise AgentError(f"{stage}: stop_reason={response.stop_reason}", usage=usage)
        return AgentCall(response.parsed_output, usage)
