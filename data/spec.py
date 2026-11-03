"""Turns scenarios.yaml into a fixed list of case briefs, each carrying its intended label.

Sampling is seeded, so the same config always yields the same briefs. The writer model
later turns each brief into message text, but the label is decided here, before any
text exists, which is what lets the eval score against it.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from triage.schemas import Category, Channel, ContactRole, EscalationFlag, GoldLabel, Priority, Queue, most_urgent

DATA_DIR = Path(__file__).resolve().parent
SCENARIOS = DATA_DIR / "scenarios.yaml"

# The handoff's eight hard-case types. Earlier entries get the extra case when the
# hard total doesn't divide evenly.
HARD_TYPES = [
    "ambiguous_category",
    "injection",
    "angry_caps",
    "short_sms",
    "non_english",
    "pm_vs_resident",
    "two_issues",
    "duplicate_followup",
]

# The starter mapping from triage/prompts/taxonomy.md. A test keeps the two in sync.
QUEUE_BY_CATEGORY = {
    Category.ACCESS_LOCKS: Queue.TIER1_ACCESS,
    Category.MOVE_IN_OUT: Queue.TIER1_ACCESS,
    Category.THERMOSTAT_HVAC: Queue.TIER1_DEVICES,
    Category.HUB_CONNECTIVITY: Queue.TIER1_DEVICES,
    Category.LEAK_SENSOR: Queue.TIER1_DEVICES,
    Category.RESIDENT_APP: Queue.TIER1_ACCOUNTS,
    Category.PM_ADMIN: Queue.PM_SUCCESS,
    Category.BILLING: Queue.PM_SUCCESS,
    Category.HARDWARE_RMA: Queue.HARDWARE_RMA,
    Category.SPAM_OTHER: Queue.HUMAN_REVIEW,
}


def queue_for(category: Category, flags: list[EscalationFlag]) -> Queue:
    """Safety and outages go to Tier 2 whatever the category; everything else follows the category."""
    if EscalationFlag.SAFETY in flags or EscalationFlag.PROPERTY_WIDE_OUTAGE in flags:
        return Queue.TIER2_ESCALATIONS
    return QUEUE_BY_CATEGORY[category]


PROPERTIES = {
    "NG-0101": "Alder Court",
    "NG-0102": "Cobalt Yards",
    "NG-0103": "Fernbrook Commons",
    "NG-0104": "Harlow Point",
    "NG-0105": "Juniper Flats",
    "NG-0106": "Kestrel Row",
    "NG-0107": "Marlow Station",
    "NG-0108": "Tessa Lofts",
}

FIRST_NAMES = [
    "Maya", "Darnell", "Priya", "Luis", "Hannah", "Kwame", "Mei", "Andre", "Sofia", "Tyler",
    "Aisha", "Ben", "Camila", "Dmitri", "Ebony", "Farid", "Grace", "Hector", "Imani", "Jae",
    "Keisha", "Liam", "Mariana", "Nate", "Olu", "Paige", "Quinn", "Rosa", "Sam", "Tanvi",
    "Uriel", "Vanessa", "Wes", "Ximena", "Yusuf", "Zoe", "Thu", "Minh", "Joon", "Celine",
]

TONES = ["polite", "matter-of-fact", "frustrated", "anxious", "chatty and a bit rambling", "brisk"]

# How carefully the sender writes. The 12-case trial (2026-10-23) came back too tidy:
# eight of twelve opened "Hi, this is <name> in unit <n>" and almost none had a typo.
POLISH = [
    "careful: full sentences and normal punctuation",
    "casual: relaxed grammar and a typo or two",
    "sloppy: mostly lowercase, typos, run-on sentences, little punctuation",
]


SampleTier = Literal["P1", "P2", "P3", "P4"]


class Scenario(BaseModel):
    category: Category
    priority: Priority
    premise: str
    flags: list[EscalationFlag] = []
    role: ContactRole
    alt: Category | None = None  # ambiguous cases: the category a reasonable reader might pick instead
    # A scenario relabeled after generation keeps being sampled as its original priority, so a
    # label correction changes that scenario's labels and nothing else. Without it, moving one
    # scenario between priority pools shifts the random stream and reshuffles every brief.
    # Sampling tiers keep P4: taxonomy v0.6 folded P4 into P3 (2026-11-03), and those scenarios
    # carry sampled_as: P4 so the 200 briefs and the split stay exactly as they were.
    sampled_as: SampleTier | None = None
    # Same idea for category: a scenario relabeled into another category stays in its original
    # list in scenarios.yaml (which is what sampling draws from) and carries the new label.
    sampled_in: Category | None = None

    @property
    def sample_priority(self) -> SampleTier:
        return self.sampled_as or self.priority.value

    @property
    def sample_category(self) -> Category:
        return self.sampled_in or self.category


class ScenarioBook(BaseModel):
    scenarios: dict[Category, list[Scenario]]
    category_weights: dict[Category, float]
    priority_weights: dict[SampleTier, float]
    hard: dict[str, list[Scenario]]
    languages: list[str]
    injection_styles: list[str]
    held_out_injection_styles: list[str]
    followup_styles: list[str]


class CaseBrief(BaseModel):
    """Everything the writer needs, plus the label it doesn't see."""

    case_id: str
    scenario: Scenario
    hard_type: str | None
    flags: list[EscalationFlag]  # the scenario's flags plus any the hard type adds
    channel: Channel
    contact_role: ContactRole
    property_id: str
    property_name: str
    sender_name: str
    unit: str
    received_at: datetime
    tone: str
    length: str
    polish: str
    mentions_unit: bool  # residents sometimes leave it out, and support has to ask
    language: str = "English"
    secondary: str | None = None  # two_issues: the less urgent second problem
    special: str | None = None  # extra writing instruction for some hard types
    held_out_style: bool = False  # injection phrased in a style only the test set uses

    @property
    def label(self) -> GoldLabel:
        s = self.scenario
        priority = s.priority
        if EscalationFlag.LEGAL_OR_THREAT in self.flags:  # taxonomy v0.6: a legal threat is at least P2
            priority = most_urgent(priority, Priority.P2)
        return GoldLabel(
            category=s.category, priority=priority, queue=queue_for(s.category, self.flags), escalation_flags=self.flags
        )


def load_book(path: Path = SCENARIOS) -> ScenarioBook:
    raw = yaml.safe_load(path.read_text())
    roles = raw["default_roles"]

    def build(category: str, entry: dict) -> Scenario:
        label_category = entry.pop("category", category)  # an explicit category relabels; the list still samples
        return Scenario(
            category=label_category,
            sampled_in=category if label_category != category else None,
            role=entry.pop("role", roles.get(category, "Resident")),
            **entry,
        )

    scenarios = {cat: [build(cat, dict(e)) for e in entries] for cat, entries in raw["scenarios"].items()}
    hard = {
        kind: [build(e.pop("category"), e) for e in (dict(x) for x in entries)]
        for kind, entries in raw["hard_scenarios"].items()
    }
    mods = raw["modifiers"]
    return ScenarioBook(
        scenarios=scenarios,
        category_weights=raw["category_weights"],
        priority_weights=raw["priority_weights"],
        hard=hard,
        languages=mods["languages"],
        injection_styles=mods["injection_styles"],
        held_out_injection_styles=mods["held_out_injection_styles"],
        followup_styles=mods["followup_styles"],
    )


def brief_stratum(b: CaseBrief) -> str:
    return b.hard_type or f"ordinary {b.scenario.sample_priority}"


def split_ids(entries: list[tuple[str, str]], test_size: int, seed: int) -> set[str]:
    """Picks test-set ids from (case_id, stratum) pairs, about test_size/len(entries) of each stratum.

    Strata come from the brief alone (hard type, or priority for ordinary cases), so the
    split is known before any text is written. data/split.py applies the same function to
    the generated file, which is what lets held-out injection phrasing go only to test.
    """
    rng = random.Random(seed)
    groups: dict[str, list[str]] = {}
    for case_id, stratum in sorted(entries):
        groups.setdefault(stratum, []).append(case_id)
    for members in groups.values():
        rng.shuffle(members)

    share = test_size / len(entries)
    quota = {key: max(1, round(len(members) * share)) for key, members in groups.items()}
    # Rounding drifts off the target; settle the difference in the biggest ordinary strata,
    # so the small hard-case strata keep their share.
    ordinary = sorted((k for k in groups if k.startswith("ordinary")), key=lambda k: -len(groups[k]))
    i = 0
    while sum(quota.values()) != test_size:
        key = ordinary[i % len(ordinary)]
        quota[key] += 1 if sum(quota.values()) < test_size else -1
        i += 1
    return {case_id for key, members in groups.items() for case_id in members[: quota[key]]}


def _injection_special(style: str, priority: str) -> str:
    target = "P4, not urgent" if priority == Priority.P1 else "P1 and urgent"
    return f"Describe the real problem accurately, and also include {style.format(target=target)}."


def _spread(total: int, kinds: list[str]) -> dict[str, int]:
    base, extra = divmod(total, len(kinds))
    return {kind: base + (1 if i < extra else 0) for i, kind in enumerate(kinds)}


def _rank(p: str) -> int:
    return int(p[1:])


def _bases(rng: random.Random, book: ScenarioBook, kind: str, k: int) -> list[Scenario]:
    """Which scenarios each hard type is built on."""
    everyday = [s for cat, ss in book.scenarios.items() if cat != Category.SPAM_OTHER for s in ss]
    if kind in book.hard:
        return rng.sample(book.hard[kind], k)
    if kind == "short_sms":
        texting = {Category.ACCESS_LOCKS, Category.LEAK_SENSOR, Category.THERMOSTAT_HVAC, Category.HUB_CONNECTIVITY, Category.RESIDENT_APP}
        return rng.sample([s for s in everyday if s.sample_category in texting and s.role == ContactRole.RESIDENT], k)
    if kind == "angry_caps":  # mostly low-stakes problems, so tone and urgency pull apart
        high = [s for s in everyday if _rank(s.sample_priority) <= 2]
        low = [s for s in everyday if _rank(s.sample_priority) >= 3]
        n_low = round(k * 2 / 3)
        return rng.sample(low, n_low) + rng.sample(high, k - n_low)
    if kind == "injection":  # a couple try to talk a real P1 down; the rest try to talk a small issue up
        p1 = [s for s in everyday if s.sample_priority == Priority.P1]
        rest = [s for s in everyday if s.sample_priority != Priority.P1]
        n_down = min(2, k)
        return rng.sample(p1, n_down) + rng.sample(rest, k - n_down)
    return rng.sample(everyday, k)


def _received_at(rng: random.Random, premise: str) -> datetime:
    day = datetime(2026, 10, 5) + timedelta(days=rng.randrange(14))
    text = premise.lower()
    if "tonight" in text or "late at night" in text:
        hour = rng.choice([21, 22, 23, 0, 1])
    elif "this morning" in text:
        hour = rng.randint(7, 11)
    elif rng.random() < 0.15:
        hour = rng.choice([22, 23, 0, 1, 2, 5])
    else:
        hour = rng.randint(7, 21)
    return day.replace(hour=hour, minute=rng.randrange(60))


def _channel(rng: random.Random, scenario: Scenario, kind: str | None) -> Channel:
    if kind == "short_sms":
        return Channel.SMS
    if scenario.category == Category.SPAM_OTHER:
        return Channel.EMAIL
    if scenario.role == ContactRole.PROPERTY_MANAGER:
        return rng.choices([Channel.EMAIL, Channel.WEB], [75, 25])[0]
    if scenario.role == ContactRole.UNKNOWN:
        return rng.choices([Channel.EMAIL, Channel.WEB], [60, 40])[0]
    return rng.choices([Channel.EMAIL, Channel.SMS, Channel.WEB], [40, 30, 30])[0]


def _length(rng: random.Random, channel: Channel, kind: str | None) -> str:
    if kind == "short_sms":
        return "under 60 characters, lowercase, little or no punctuation, like a hurried text"
    if channel == Channel.SMS:
        return "one to three short sentences, informal"
    if channel == Channel.WEB:
        return "two to four sentences"
    return rng.choice(["two or three sentences", "three to five sentences", "one long, rambling paragraph"])


def _make(
    rng: random.Random, book: ScenarioBook, number: int, scenario: Scenario, kind: str | None, nth: int
) -> CaseBrief:
    flags = list(scenario.flags)
    channel = _channel(rng, scenario, kind)
    tone = rng.choice(TONES)
    language, secondary, special = "English", None, None

    if kind == "angry_caps":
        tone = "angry, with some words or whole sentences in ALL CAPS"
        special = "The anger is about the hassle. Keep the facts exactly as described; don't add new problems or urgency."
        if nth % 3 == 0:
            if EscalationFlag.LEGAL_OR_THREAT not in flags:  # the billing-attorney scenario already has it
                flags.append(EscalationFlag.LEGAL_OR_THREAT)
            special += " Include a threat to get a lawyer involved or to file a formal complaint."
    elif kind == "injection":
        flags.append(EscalationFlag.INJECTION_ATTEMPT)
        special = _injection_special(book.injection_styles[nth % len(book.injection_styles)], scenario.sample_priority)
    elif kind == "non_english":
        language = book.languages[nth % len(book.languages)]
    elif kind == "duplicate_followup":
        style = book.followup_styles[nth % len(book.followup_styles)]
        special = f"Write it as {style}. Restate the problem so the message stands on its own."
    elif kind == "two_issues":
        others = [
            s
            for cat, ss in book.scenarios.items()
            if cat not in (scenario.sample_category, Category.SPAM_OTHER)
            for s in ss
            if _rank(s.sample_priority) >= _rank(scenario.sample_priority) and not s.flags and s.role == scenario.role
        ]
        if others:
            secondary = rng.choice(others).premise
    elif kind == "pm_vs_resident":
        special = "Make it clear who is writing and on whose behalf, the way that person naturally would."
    elif kind == "ambiguous_category":
        special = "Describe it the way the sender experiences it. Don't diagnose the cause."

    if kind == "short_sms":
        polish = POLISH[2]
    else:
        polish = rng.choices(POLISH, [20, 40, 40] if channel == Channel.SMS else [45, 35, 20])[0]

    property_id = rng.choice(sorted(PROPERTIES))
    return CaseBrief(
        case_id=f"NG-{number:03d}",
        scenario=scenario,
        hard_type=kind,
        flags=flags,
        channel=channel,
        contact_role=scenario.role,
        property_id=property_id,
        property_name=PROPERTIES[property_id],
        sender_name=rng.choice(FIRST_NAMES),
        unit=f"{rng.randint(1, 4)}{rng.choice('ABCDEF')}" if rng.random() < 0.5 else f"{rng.randint(1, 4)}{rng.randint(1, 24):02d}",
        received_at=_received_at(rng, scenario.premise),
        tone=tone,
        length=_length(rng, channel, kind),
        polish=polish,
        mentions_unit=rng.random() < 0.75,
        language=language,
        secondary=secondary,
        special=special,
    )


def sample_briefs(n: int, hard_share: float, seed: int, test_size: int, path: Path = SCENARIOS) -> list[CaseBrief]:
    rng = random.Random(seed)
    book = load_book(path)

    plans: list[tuple[Scenario, str | None, int]] = []
    for kind, k in _spread(round(n * hard_share), HARD_TYPES).items():
        plans += [(s, kind, nth) for nth, s in enumerate(_bases(rng, book, kind, k))]

    categories = list(book.category_weights)
    weights = [book.category_weights[c] for c in categories]
    for nth in range(n - len(plans)):
        category = rng.choices(categories, weights)[0]
        options = book.scenarios[category]
        pick = rng.choices(options, [book.priority_weights[s.sample_priority] for s in options])[0]
        plans.append((pick, None, nth))

    rng.shuffle(plans)
    briefs = [_make(rng, book, i + 1, scenario, kind, nth) for i, (scenario, kind, nth) in enumerate(plans)]

    # Injection cases bound for the test set get phrasings dev never sees. This runs after
    # sampling and draws no random numbers, so every other brief is unchanged.
    test = split_ids([(b.case_id, brief_stratum(b)) for b in briefs], test_size, seed)
    held = [b for b in briefs if b.hard_type == "injection" and b.case_id in test]
    for k, b in enumerate(held):
        style = book.held_out_injection_styles[k % len(book.held_out_injection_styles)]
        b.special = _injection_special(style, b.scenario.sample_priority)
        b.held_out_style = True
    return briefs


def describe(briefs: list[CaseBrief]) -> dict[str, dict[str, int]]:
    """Counts by priority, category, channel, and hard type, for a quick look at the mix."""
    return {
        "priority": dict(sorted(Counter(b.scenario.priority.value for b in briefs).items())),
        "category": dict(Counter(b.scenario.category.value for b in briefs).most_common()),
        "channel": dict(Counter(b.channel.value for b in briefs).most_common()),
        "hard_type": dict(Counter(b.hard_type or "none" for b in briefs).most_common()),
    }


if __name__ == "__main__":
    import json

    from triage.config import load_settings

    g = load_settings().generator
    print(json.dumps(describe(sample_briefs(g.cases, g.hard_share, g.seed, g.test_size)), indent=2))
