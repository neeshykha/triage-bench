"""Hard rules that override the model. This file is the answer to "where shouldn't AI decide?"

Two principles hold for every rule here:

1. Rules only raise urgency. A rule can force a case up to P1; no rule can talk one
   down. Missing a P1 costs far more than over-triaging a P3, so the floor only
   works in one direction.
2. Rules read the raw case text, never the model's confidence or reasoning, so a
   persuasive rationale can't argue past them.

`precheck` runs before any model call and turns the case text into `Signals`.
`apply_rules` runs after every agent output and returns the corrected result plus
a log of what changed, so the eval can count every override.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time

from .config import RulesSettings
from .schemas import Case, Channel, EscalationFlag, Priority, RuleOverride, TriageResult

# --- Phrase lists. Matched against lowercased text with straight apostrophes. ---

_NEG = r"(?:won'?t|wont|will not|doesn'?t|does not|isn'?t|is not|not)"
_CANT = r"(?:cannot|can ?not|can'?t|cant|unable to)"

LOCKOUT = [
    r"\blocked out\b",
    r"\block(?:ed)? me out\b",
    rf"\b{_CANT} get in(?:side)?\b",
    rf"\b{_CANT} get into (?:my|the|our) (?:apartment|apt|unit|home|place|building|room)\b",
    rf"\b(?:door|lock|deadbolt|keypad)\b[^.!?\n]{{0,30}}\b{_NEG} (?:open|unlock|work|respond|accept)\w*",
    rf"\b(?:code|pin|fob|key ?card|mobile key)\b[^.!?\n]{{0,30}}\b{_NEG} (?:open|unlock|work)\w*",
    r"\bstuck (?:outside|in the hall(?:way)?|at the door)\b",
]

# "I'm not locked out" is how people say a lock problem isn't urgent. Three dev cases
# (2026-10-23) said it and R1 forced them to P1. These phrases are removed before the
# lockout search. The list is explicit on purpose: "\w+nt" would also eat "tenant locked out".
NEGATED_LOCKOUT = r"\b(?:not|never|isn'?t|aren'?t|wasn'?t|weren'?t)\s+(?:\w+\s+){0,2}?locked out\b"

# Says the lockout is over. Learned from claude-triage-simulator, where "resolved
# lockout still marked P1" was the classifier's clearest weak spot.
LOCKOUT_RESOLVED = [
    r"\b(?:i|we) got in\b",
    r"\bgot (?:inside|back in)\b",
    r"\b(?:i'?m|im|we'?re) (?:back )?in(?:side)? now\b",
    r"\b(?:never ?mind|nvm)\b",
    r"\b(?:backup|back-up|temporary|temp|spare) (?:code|key|fob)\b[^.!?\n]{0,20}\bworked\b",
    r"\b(?:was|were) locked out\b",
    r"\b(?:is|it'?s|its) (?:working|fine|ok|okay) now\b",
]

# Overrides a resolution phrase: "got in earlier, locked out again" is still a lockout.
# Tied to the lockout itself, because a bare "again" also matches "will it happen again?"
LOCKOUT_ONGOING = [
    r"\bstill (?:locked out|can'?t get in|cant get in|outside|stuck)\b",
    r"\b(?:locked out|lock(?:ed)? me out)(?: \w+){0,3}? again\b",
]

ACTIVE_WATER = [
    r"\b(?:water|leak(?:ing)?)\b[^.!?\n]{0,40}\b(?:everywhere|on the floor|all over|pouring|gushing|spraying|flooding|coming through|dripping|pooling)\b",
    r"\b(?:flood(?:ed|ing)?|burst pipe|pipe burst)\b",
    r"\b(?:is|are|it'?s|its|keeps|still) leaking\b",
    r"\bleaking (?:right )?now\b",
    r"\bwater (?:is |keeps )?(?:coming|leaking|dripping|pouring|running) (?:in|out|through|from|down)\b",
]

# Says there's no water after all: a false alarm, a dry floor, or a leak already fixed.
NO_WATER = [
    r"\bno (?:water|leak)\b",
    r"\bfalse alarm\b",
    r"\b(?:is|was|it'?s|its|everything'?s|floor'?s) (?:completely |totally |bone )?dry\b",
    r"\bnothing (?:is )?(?:wet|leaking)\b",
    r"\b(?:isn'?t|is not|wasn'?t|was not|aren'?t) (?:any )?(?:water|leak(?:ing)?)\b",
    r"\b(?:leak|water|it) (?:has |is )?(?:stopped|been fixed|been repaired)\b",
    r"\b(?:plumber|maintenance) (?:fixed|repaired|stopped) (?:it|the leak)\b",
]

# Overrides a no-water phrase: "plumber fixed it, now it's leaking again" is still active.
WATER_ONGOING = [
    r"\bstill (?:leaking|dripping|flooding|wet)\b",
    r"\b(?:leak\w*|dripping|flooding)(?: \w+){0,3}? again\b",
]

URGENCY = [
    r"\b(?:right now|now|asap|urgent(?:ly)?|emergency|immediately|tonight|help|waiting|stuck)\b",
    r"\b(?:freezing|raining|late|midnight)\b",
    r"\b(?:kids?|baby|child|children|dog|cat|pet|medication|meds)\b",
]

# Text aimed at the triage system rather than at support. Broadened 2026-10-23 on the six
# dev injection cases; the test set's injections use phrasings these were never checked against.
INJECTION = [
    r"\b(?:ignore|disregard) (?:all |any |the |your )?(?:previous |prior |above |earlier |preceding )?(?:instructions|rules|directions|guidelines|prompts?)\b",
    r"\b(?:mark|set|classify|label|tag|route|assign|make|treat|flag) (?:this|it|the|my)(?: \w+){0,3}? (?:as|to) (?:a )?p[1-4]\b",
    r"\bthis (?:\w+ )?is (?:a )?p[1-4]\b",
    r"\b(?:system|admin|administrator) override\b",
    r"\b(?:note|message|instructions?) (?:to|for) (?:the )?(?:\w+ )?(?:ai|bot|assistant|system)\b",
    r"\bto the (?:ai|bot|assistant)\b",
    r"\btriage (?:bot|system|rules|ai)\b",
    r"\b(?:system prompt|developer message|new instructions)\b",
    r"\byou are (?:now )?an? (?:ai|language model|llm|assistant)\b",
]


def _normalize(text: str) -> str:
    return text.lower().replace("’", "'").replace("‘", "'")


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        if m := re.search(pattern, text):
            return m.group(0)
    return None


def _is_after_hours(t: time, window: RulesSettings) -> bool:
    start, end = window.after_hours_start, window.after_hours_end
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window wraps midnight


# --- Pre-check ---


@dataclass(frozen=True)
class Signals:
    """What the deterministic pre-check found in the raw case, before any model ran."""

    lockout: str | None  # the phrase that matched, kept for the override log
    lockout_resolved: bool
    time_signal: str | None
    active_water: str | None  # None when nothing matched, or a no-water phrase cancelled it
    injection: str | None


def precheck(case: Case, window: RulesSettings) -> Signals:
    text = _normalize(f"{case.subject or ''}\n{case.body}")

    resolved = _first_match(LOCKOUT_RESOLVED, text) is not None and _first_match(LOCKOUT_ONGOING, text) is None

    active_water = _first_match(ACTIVE_WATER, text)
    if active_water and _first_match(NO_WATER, text) and _first_match(WATER_ONGOING, text) is None:
        active_water = None

    time_signal = _first_match(URGENCY, text)
    if time_signal is None and _is_after_hours(case.received_at.time(), window):
        time_signal = f"received {case.received_at:%H:%M}, after hours"
    if time_signal is None and case.channel == Channel.SMS:
        time_signal = "sent by SMS, a real-time channel"

    return Signals(
        lockout=_first_match(LOCKOUT, re.sub(NEGATED_LOCKOUT, " ", text)),
        lockout_resolved=resolved,
        time_signal=time_signal,
        active_water=active_water,
        injection=_first_match(INJECTION, text),
    )


# --- Rules. Each returns (field, new value, reason) when it fires, else None. ---

Fix = tuple[str, object, str]


def lockout_now(signals: Signals, result: TriageResult) -> Fix | None:
    """R1: an ongoing lockout with any time-sensitive signal is P1."""
    if signals.lockout and not signals.lockout_resolved and signals.time_signal and result.priority != Priority.P1:
        return "priority", Priority.P1, f"ongoing lockout ('{signals.lockout}') + time signal ({signals.time_signal})"
    return None


def active_leak(signals: Signals, result: TriageResult) -> Fix | None:
    """R2: water actively escaping is P1, whatever category the model chose.

    Narrowed from "any Leak/Sensor Alert is P1" (Aneesh, 2026-10-22) so low-battery chirps
    and false alarms stay with the model. A sensor alarm with no mention of water is also
    left to the model.
    """
    if signals.active_water and result.priority != Priority.P1:
        return "priority", Priority.P1, f"active water in the case ('{signals.active_water}')"
    return None


def injection_flag(signals: Signals, result: TriageResult) -> Fix | None:
    """R3: flag text that tries to instruct the system. Flag only; it never changes routing."""
    if signals.injection and EscalationFlag.INJECTION_ATTEMPT not in result.escalation_flags:
        flags = [*result.escalation_flags, EscalationFlag.INJECTION_ATTEMPT]
        return "escalation_flags", flags, f"instruction-like text in the case ('{signals.injection}')"
    return None


RULES: dict[str, Callable[[Signals, TriageResult], Fix | None]] = {
    "R1_lockout_now": lockout_now,
    "R2_active_leak": active_leak,
    "R3_injection_flag": injection_flag,
}


def _show(value: object) -> str:
    return ", ".join(value) if isinstance(value, list) else str(value)


def apply_rules(signals: Signals, result: TriageResult, stage: str) -> tuple[TriageResult, list[RuleOverride]]:
    overrides = []
    for rule_id, rule in RULES.items():
        if fix := rule(signals, result):
            field, value, reason = fix
            overrides.append(
                RuleOverride(
                    rule_id=rule_id,
                    stage=stage,
                    field=field,
                    before=_show(getattr(result, field)),
                    after=_show(value),
                    reason=reason,
                )
            )
            result = result.model_copy(update={field: value})
    return result, overrides
