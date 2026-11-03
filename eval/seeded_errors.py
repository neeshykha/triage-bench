"""Can the challenger catch a wrong triage decision? A seeded-error test with a known answer key.

    python -m eval.seeded_errors --max-usd 1

In 140 dev cases the challenger never disputed a routing decision, which could mean it
can't catch errors or that there were almost none to catch. This answers that directly.
It takes dev cases where a saved pipeline D run's first pass matched the label on every
routing field with no rule help, keeps half as they are, and breaks the other half in one
targeted way. Then it asks the challenger alone to review all of them; no triage calls.

Scoring: on a broken decision, did it object to routing at all (flagged), name the broken
field (pinpointed), and suggest the right value (fixed)? On a clean one, any routing
objection is a false alarm. Caveat: each draft reply was written for the correct decision,
so a P1 pushed down to P3 still carries an urgent draft, a clue a real slip wouldn't leave.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich.console import Console

from data.spec import queue_for
from triage.agents import AgentError, Agents, ClaudeAgents
from triage.config import load_settings
from triage.run import RUNS_DIR, load_cases, load_run, preflight
from triage.schemas import Category, EscalationFlag, LabeledCase, Priority, Queue, TriageResult

ROOT = Path(__file__).resolve().parent.parent
KINDS = ["under_triage", "over_triage", "wrong_category", "wrong_queue", "dropped_flag"]
ROUTING = {"category", "priority", "queue", "escalation_flags"}

# Clearly wrong categories, so a broken decision can't land on a defensible alternative.
FAR_CATEGORY = {
    Category.ACCESS_LOCKS: Category.BILLING,
    Category.THERMOSTAT_HVAC: Category.PM_ADMIN,
    Category.HUB_CONNECTIVITY: Category.BILLING,
    Category.LEAK_SENSOR: Category.RESIDENT_APP,
    Category.RESIDENT_APP: Category.HARDWARE_RMA,
    Category.MOVE_IN_OUT: Category.BILLING,
    Category.PM_ADMIN: Category.LEAK_SENSOR,
    Category.HARDWARE_RMA: Category.RESIDENT_APP,
    Category.BILLING: Category.ACCESS_LOCKS,
    Category.SPAM_OTHER: Category.THERMOSTAT_HVAC,
}
ROUTINE_QUEUES = [Queue.TIER1_ACCESS, Queue.TIER1_DEVICES, Queue.TIER1_ACCOUNTS, Queue.PM_SUCCESS, Queue.HARDWARE_RMA]
LEVELS = list(Priority)  # P1-P4 when the seeded test ran (2026-10-26); P4 folded into P3 in v0.6


@dataclass
class Item:
    case: LabeledCase
    proposal: TriageResult  # what the challenger reviews
    kind: str  # "clean" or one of KINDS
    field: str | None  # the field the break targets
    correct: str | None  # its right value
    # Every routing field the break made wrong, with its right value. A dropped safety or outage
    # flag also moves the queue off Tier 2, and a wrong category moves the queue too, so an
    # objection to either field has spotted the break.
    wrong: dict[str, str] | None = None


def clean_first_passes(run_dirs: list[Path], dev: dict[str, LabeledCase]) -> list[tuple[LabeledCase, TriageResult]]:
    """First passes that match today's dev label on every routing field, with no rule override."""
    out, seen = [], set()
    for run_dir in run_dirs:
        for lc, r in load_run(run_dir):
            step = next((s for s in r.trace if s.stage == "triage"), None)
            case = dev.get(lc.case.case_id)
            if step is None or case is None or case.case.case_id in seen or r.rule_overrides:
                continue
            first, gold = TriageResult.model_validate(step.data), case.label
            if (first.category, first.priority, first.queue, sorted(first.escalation_flags)) == (
                gold.category, gold.priority, gold.queue, sorted(gold.escalation_flags)
            ):
                out.append((case, first))
                seen.add(case.case.case_id)
    return sorted(out, key=lambda x: x[0].case.case_id)


def corrupt(kind: str, p: TriageResult) -> tuple[TriageResult, str, str] | None:
    """One targeted break, or None when this decision can't carry it."""
    i = LEVELS.index(p.priority)
    if kind == "under_triage" and i + 2 < len(LEVELS):
        return p.model_copy(update={"priority": LEVELS[i + 2]}), "priority", p.priority.value
    if kind == "over_triage" and i >= 2:
        return p.model_copy(update={"priority": LEVELS[i - 2]}), "priority", p.priority.value
    if kind == "wrong_category":
        wrong = FAR_CATEGORY[p.category]
        return p.model_copy(update={"category": wrong, "queue": queue_for(wrong, p.escalation_flags)}), "category", p.category.value
    if kind == "wrong_queue" and p.queue in ROUTINE_QUEUES:
        wrong = next(q for q in ROUTINE_QUEUES[ROUTINE_QUEUES.index(p.queue) + 1 :] + ROUTINE_QUEUES if q != p.queue)
        return p.model_copy(update={"queue": wrong}), "queue", p.queue.value
    if kind == "dropped_flag" and p.escalation_flags:
        dropped = p.escalation_flags[0]
        rest = [f for f in p.escalation_flags if f != dropped]
        queue = queue_for(p.category, rest)  # keep the queue consistent with what's left, so only the flag is wrong
        return p.model_copy(update={"escalation_flags": rest, "queue": queue}), "escalation_flags", dropped.value
    return None


def wrong_fields(original: TriageResult, broken: TriageResult) -> dict[str, str]:
    wrong = {f: getattr(original, f).value for f in ("category", "priority", "queue") if getattr(original, f) != getattr(broken, f)}
    dropped = [f.value for f in original.escalation_flags if f not in broken.escalation_flags]
    if dropped:
        wrong["escalation_flags"] = dropped[0]
    return wrong


def build_items(pool: list[tuple[LabeledCase, TriageResult]], per_kind: int, seed: int) -> list[Item]:
    """per_kind broken decisions of each kind plus as many clean ones, each case used once."""
    rng = random.Random(seed)
    order = pool[:]
    rng.shuffle(order)
    used: set[str] = set()
    items: list[Item] = []
    for kind in KINDS:
        made = 0
        for case, first in order:
            if made == per_kind:
                break
            if case.case.case_id in used or (broken := corrupt(kind, first)) is None:
                continue
            proposal, field, correct = broken
            items.append(Item(case, proposal, kind, field, correct, wrong_fields(first, proposal)))
            used.add(case.case.case_id)
            made += 1
    n_broken = len(items)
    for case, first in order:
        if len(items) == 2 * n_broken:
            break
        if case.case.case_id not in used:
            items.append(Item(case, first, "clean", None, None))
            used.add(case.case.case_id)
    rng.shuffle(items)
    return items


def score(item: Item, verdict: str, objections: list[dict]) -> dict:
    routing = [o for o in objections if o["field"] in ROUTING]
    wrong = item.wrong or ({item.field: item.correct} if item.field else {})
    on_field = [o for o in routing if o["field"] in wrong]
    # Containment, not equality: "P1 (active lockout)" or "safety, injection_attempt" still count.
    fixed = any(wrong[o["field"]].lower() in (o.get("suggested_value") or "").lower() for o in on_field)
    return {
        "flagged": bool(routing),
        "pinpointed": bool(on_field),
        "fixed": fixed,
        "draft_objection": any(o["field"] == "draft_first_response" for o in objections),
    }


def summarize(rows: list[dict]) -> dict:
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        c = by_kind[r["kind"]]
        c["n"] += 1
        for k in ("flagged", "pinpointed", "fixed", "draft_objection", "error"):
            c[k] += int(bool(r.get(k)))
    broken = [r for r in rows if r["kind"] != "clean" and not r.get("error")]
    clean = [r for r in rows if r["kind"] == "clean" and not r.get("error")]
    return {
        "broken": len(broken),
        "caught": sum(r["flagged"] for r in broken),
        "pinpointed": sum(r["pinpointed"] for r in broken),
        "fixed": sum(r["fixed"] for r in broken),
        "clean": len(clean),
        "false_alarms": sum(r["flagged"] for r in clean),
        "errors": sum(1 for r in rows if r.get("error")),
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
    }


def run_items(items: list[Item], agents: Agents, max_usd: float, workers: int = 4, on_row=None) -> tuple[list[dict], str | None]:
    rows: list[dict] = []
    stopped = None

    def review(item: Item) -> dict:
        start = time.perf_counter()
        base = {"case_id": item.case.case.case_id, "kind": item.kind, "field": item.field, "correct": item.correct, "wrong": item.wrong,
                "shown": item.proposal.for_review()}
        try:
            call = agents.challenge(item.case.case, item.proposal)
        except AgentError as err:
            return base | {"error": str(err), "cost_usd": err.usage.cost_usd if err.usage else 0.0, "latency_s": time.perf_counter() - start}
        out = call.output.model_dump(mode="json")
        return base | {"verdict": out["verdict"], "objections": out["objections"], "severity": out["severity"],
                       "cost_usd": call.usage.cost_usd, "latency_s": call.usage.latency_s} | score(item, out["verdict"], out["objections"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = list(items)
        while pending and stopped is None:
            spent = sum(r["cost_usd"] for r in rows)
            if spent >= max_usd:
                stopped = f"spend cap of ${max_usd:.2f} reached"
                break
            batch, pending = pending[:workers], pending[workers:]
            try:
                for row in pool.map(review, batch):
                    rows.append(row)
                    if on_row:
                        on_row(row)
            except anthropic.APIError as err:
                stopped = f"{type(err).__name__}: {err}"
    return rows, stopped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-usd", type=float, required=True)
    parser.add_argument("--per-kind", type=int, default=4, help="broken decisions of each kind (5 kinds), plus as many clean")
    parser.add_argument("--runs", nargs="*", type=Path, help="saved pipeline D run folders to draw first passes from (default: every D run on dev)")
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--dry-run", action="store_true", help="build and print the item plan; no API calls")
    args = parser.parse_args(argv)
    console = Console()

    dev = {c.case.case_id: c for c in load_cases(ROOT / "data" / "cases_dev.jsonl")}
    run_dirs = [p for p in (args.runs or sorted(RUNS_DIR.glob("*-D-cases_dev"))) if (p / "results.jsonl").exists()]
    pool = clean_first_passes(run_dirs, dev)
    items = build_items(pool, args.per_kind, args.seed)
    if args.dry_run:
        console.print(f"{len(pool)} verified first passes from {len(run_dirs)} runs → {len(items)} items: "
                      f"{dict(Counter(i.kind for i in items))}")
        for i in items:
            console.print(f"  {i.case.case.case_id} {i.kind:15} {i.field or '':17} right={i.correct or ''}")
        return 0

    load_dotenv(ROOT / ".env")
    if reason := preflight(os.environ):
        console.print(f"[red]{reason}[/]")
        return 2
    settings = load_settings()
    console.print(
        f"[bold]Seeded-error test[/]: {sum(i.kind != 'clean' for i in items)} broken + {sum(i.kind == 'clean' for i in items)} clean "
        f"decisions from {len(pool)} verified first passes · challenger {settings.agents['challenger'].model} "
        f"({settings.agents['challenger'].effort}) · cap ${args.max_usd:.2f}"
    )

    out_dir = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-seeded-errors"
    out_dir.mkdir(parents=True)

    def progress(row: dict) -> None:
        mark = "error" if row.get("error") else ("objects" if row["flagged"] else "agrees")
        right = "" if row["kind"] == "clean" else (" [green]caught[/]" if row.get("flagged") else " [red]missed[/]")
        console.print(f"  {row['case_id']}  {row['kind']:15} challenger {mark}{right}  [dim]${row['cost_usd']:.4f}[/]")
        with (out_dir / "results.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")

    rows, stopped = run_items(items, ClaudeAgents(settings), args.max_usd, on_row=progress)
    s = summarize(rows) | {"stopped_by": stopped, "seed": args.seed, "runs": [p.name for p in run_dirs]}
    (out_dir / "summary.json").write_text(json.dumps(s, indent=2) + "\n")

    console.print(
        f"\nBroken decisions caught: [bold]{s['caught']}/{s['broken']}[/] · named the broken field {s['pinpointed']} · "
        f"suggested the right value {s['fixed']}"
    )
    console.print(f"False alarms on clean decisions: [bold]{s['false_alarms']}/{s['clean']}[/] · errors {s['errors']} · cost ${s['cost_usd']:.4f}")
    for kind in KINDS + ["clean"]:
        k = s["by_kind"].get(kind)
        if k:
            console.print(f"  {kind:15} n={k['n']}  objected {k.get('flagged', 0)}  named field {k.get('pinpointed', 0)}  right fix {k.get('fixed', 0)}")
    console.print("[dim]Caveat: drafts were written for the correct decision, which can tip off the challenger on priority breaks.[/]")
    if stopped:
        console.print(f"[bold red]Stopped early:[/] {stopped}")
    console.print(f"\n[dim]Saved to {out_dir}[/]")
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
