"""Run one pipeline over a case file and print a readable table.

    python -m triage.run --cases data/cases_smoke.jsonl --pipeline A --max-usd 2 --drafts

This spends real API credit, so it refuses to start inside a Claude Code session and
stops before starting any case once the run's spend reaches --max-usd (one case can
carry it past the cap, never two). Every result is saved under eval/runs/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.table import Table

from .agents import Agents, ClaudeAgents
from .config import Settings, load_settings
from .orchestrator import Pipeline, run_case
from .schemas import Category, EscalationFlag, LabeledCase, PipelineResult, Priority, Queue, Route, fold_retired

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "eval" / "runs"


def load_cases(path: Path) -> list[LabeledCase]:
    return [LabeledCase.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def preflight(env: Mapping[str, str]) -> str | None:
    """Why this run shouldn't start, or None when it's clear to spend."""
    if env.get("CLAUDECODE"):
        return "Refusing to run inside a Claude Code session. This spends real API credit, so launch it from your own terminal."
    if not env.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY isn't set. Export it, or copy .env.example to .env and fill it in."
    return None


@dataclass
class RunOutcome:
    results: list[tuple[LabeledCase, PipelineResult]] = field(default_factory=list)
    not_started: int = 0
    stopped_by: str | None = None  # why the run ended early, if it did

    @property
    def spent(self) -> float:
        return sum(r.cost_usd for _, r in self.results)


def run_cases(
    cases: list[LabeledCase],
    agents: Agents,
    pipeline: Pipeline,
    max_usd: float,
    out_file: Path | None = None,
    on_result: Callable[[LabeledCase, PipelineResult], None] | None = None,
) -> RunOutcome:
    outcome = RunOutcome()
    for i, labeled in enumerate(cases):
        if outcome.spent >= max_usd:
            outcome.not_started, outcome.stopped_by = len(cases) - i, f"spend cap of ${max_usd:.2f} reached"
            break
        try:
            result = run_case(labeled.case, agents, pipeline)
        except anthropic.APIError as err:  # bad key, rate limit after retries, network
            outcome.not_started, outcome.stopped_by = len(cases) - i, f"{type(err).__name__}: {err}"
            break
        outcome.results.append((labeled, result))
        if out_file:
            with out_file.open("a") as f:
                f.write(json.dumps({"label": labeled.model_dump(mode="json"), "result": result.model_dump(mode="json")}) + "\n")
        if on_result:
            on_result(labeled, result)
    return outcome


# --- Scoring against the file's labels ---


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else 0.0


ROUTING_FIELDS = ("category", "priority", "queue", "escalation_flags")


def _routing(category, priority, queue, flags) -> tuple:
    # Traces are raw dicts, so a run from before taxonomy v0.6 can still say P4; it reads as P3.
    return (str(category), fold_retired(str(priority)), str(queue), tuple(sorted(str(f) for f in flags)))


def _first_pass(r: PipelineResult) -> tuple | None:
    """The routing the challenger first saw: triage's output after any rule override."""
    step = next((s for s in r.trace if s.stage == "triage"), None)
    if step is None:
        return None
    seen = dict(step.data)
    for o in r.rule_overrides:
        if o.stage == "triage" and o.field == "priority":
            seen["priority"] = o.after
        if o.stage == "triage" and o.field == "escalation_flags":
            seen["escalation_flags"] = [f.strip() for f in o.after.split(",") if f.strip()]
    return _routing(seen["category"], seen["priority"], seen["queue"], seen["escalation_flags"])


def challenger_stats(results: list[tuple[LabeledCase, PipelineResult]]) -> dict[str, int]:
    """Was the challenger right to object? Routing is scored against the file's labels; drafts have no label.

    Routing (category, priority, queue, flags): catch = it objected to routing that was wrong;
    false_alarm = it objected to routing that was right; miss = it let wrong routing through;
    fixed / broke = whether the final routing ended up right after a catch, or wrong after a
    false alarm. Draft objections are counted on their own, with how many triage accepted.
    The first version scored any objection against category and priority, so eight valid
    draft objections in the first live run (2026-10-26) showed up as false alarms.
    """
    s = {"challenged": 0, "catch": 0, "false_alarm": 0, "miss": 0, "agreed_right": 0, "fixed": 0, "broke": 0,
         "draft_objections": 0, "draft_accepted": 0}
    for lc, r in results:
        challenge = next((step for step in r.trace if step.stage == "challenge"), None)
        first = _first_pass(r)
        if challenge is None or first is None:
            continue
        s["challenged"] += 1
        gold = lc.label
        right = first == _routing(gold.category.value, gold.priority.value, gold.queue.value, [f.value for f in gold.escalation_flags])
        final_right = r.final is not None and _routing(
            r.final.category.value, r.final.priority.value, r.final.queue.value, [f.value for f in r.final.escalation_flags]
        ) == _routing(gold.category.value, gold.priority.value, gold.queue.value, [f.value for f in gold.escalation_flags])
        fields = {o["field"] for o in challenge.data["objections"]}
        if fields & set(ROUTING_FIELDS):
            s["catch" if not right else "false_alarm"] += 1
            s["fixed"] += int(not right and final_right)
            s["broke"] += int(right and not final_right)
        else:
            s["agreed_right" if right else "miss"] += 1
        if "draft_first_response" in fields:
            s["draft_objections"] += 1
            revise = next((step for step in r.trace if step.stage == "revise"), None)
            if revise and any(x["field"] == "draft_first_response" and x["decision"] == "accept" for x in revise.data["responses"]):
                s["draft_accepted"] += 1
    return s


def summarize(results: list[tuple[LabeledCase, PipelineResult]]) -> dict:
    n = len(results)
    scored = [(lc.label, r.final) for lc, r in results if r.final is not None]
    gold_p1 = [(g, f) for g, f in scored if g.priority == Priority.P1]
    called_p1 = [(g, f) for g, f in scored if f.priority == Priority.P1]
    injection = [(lc, r) for lc, r in results if "injection" in lc.hard_case_tags]
    latencies = [r.latency_s for _, r in results]
    usage = [u for _, r in results for u in r.usage]
    return {
        "cases": n,
        "schema_valid": sum(1 for _, r in results if r.route != Route.AGENT_ERROR),
        "category_correct": sum(1 for g, f in scored if f.category == g.category),
        "priority_correct": sum(1 for g, f in scored if f.priority == g.priority),
        "queue_correct": sum(1 for lc, r in results if r.queue == lc.label.queue),
        "p1_caught": sum(1 for g, f in gold_p1 if f.priority == Priority.P1),
        "p1_total": len(gold_p1),
        "p1_called": len(called_p1),
        "p1_called_correctly": sum(1 for g, f in called_p1 if g.priority == Priority.P1),
        "injection_flagged": sum(
            1 for _, r in injection if r.final and EscalationFlag.INJECTION_ATTEMPT in r.final.escalation_flags
        ),
        "injection_total": len(injection),
        "rule_overrides": sum(len(r.rule_overrides) for _, r in results),
        "human_review": sum(1 for _, r in results if r.route in (Route.UNRESOLVED, Route.AGENT_ERROR)),
        "revisions": sum(r.revisions for _, r in results),
        "challenger": challenger_stats(results),
        "cost_usd": round(sum(r.cost_usd for _, r in results), 4),
        "cost_per_case_usd": round(sum(r.cost_usd for _, r in results) / n, 4) if n else 0.0,
        "latency_p50_s": round(_percentile(latencies, 0.5), 2),
        "latency_p95_s": round(_percentile(latencies, 0.95), 2),
        "input_tokens": sum(u.input_tokens for u in usage),
        "output_tokens": sum(u.output_tokens for u in usage),
        "cache_read_tokens": sum(u.cache_read_input_tokens for u in usage),
    }


# --- Output. Short labels keep the table readable in an 80-column terminal. ---

SHORT = {
    Category.ACCESS_LOCKS: "Access",
    Category.THERMOSTAT_HVAC: "HVAC",
    Category.HUB_CONNECTIVITY: "Hub",
    Category.LEAK_SENSOR: "Leak",
    Category.RESIDENT_APP: "App",
    Category.MOVE_IN_OUT: "Move",
    Category.PM_ADMIN: "PM Admin",
    Category.HARDWARE_RMA: "RMA",
    Category.BILLING: "Billing",
    Category.SPAM_OTHER: "Spam",
    EscalationFlag.SAFETY: "safety",
    EscalationFlag.PROPERTY_WIDE_OUTAGE: "outage",
    EscalationFlag.LEGAL_OR_THREAT: "legal",
    EscalationFlag.INJECTION_ATTEMPT: "inject",
    Queue.TIER1_ACCESS: "T1 Access",
    Queue.TIER1_DEVICES: "T1 Devices",
    Queue.TIER1_ACCOUNTS: "T1 Accounts",
    Queue.TIER2_ESCALATIONS: "T2 Escal.",
    Queue.PM_SUCCESS: "PM Success",
    Queue.HARDWARE_RMA: "RMA",
    Queue.HUMAN_REVIEW: "Human",
    Route.NO_CHALLENGER: "-",
    Route.AGREED: "agree",
    Route.AGREED_AFTER_REVISION: "revise",
    Route.UNRESOLVED: "human",
    Route.AGENT_ERROR: "error",
}


def _mark(ok: bool, text: str) -> str:
    return f"[green]{text}[/]" if ok else f"[bold red]{text}[/]"


def results_table(results: list[tuple[LabeledCase, PipelineResult]], show_route: bool) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY, padding=(0, 1), pad_edge=False, caption="Green matches the file's label; red misses it."
    )
    for name in ("Case", "Expected", "Got", "Queue", "Flags", "Conf", "Rules", *(["Route"] if show_route else [])):
        table.add_column(name, no_wrap=name != "Flags", style="dim" if name == "Expected" else None)

    for labeled, r in results:
        gold, f = labeled.label, r.final
        queue = _mark(r.queue == gold.queue, SHORT[r.queue])
        row = [labeled.case.case_id, f"{SHORT[gold.category]} {gold.priority.value}"]
        if f is None:
            row += ["[bold red]ERROR[/]", queue, "", "", ""]
        else:
            row += [
                f"{_mark(f.category == gold.category, SHORT[f.category])} {_mark(f.priority == gold.priority, f.priority.value)}",
                queue,
                " ".join(SHORT[flag] for flag in f.escalation_flags),
                f"{f.confidence:.2f}",
                " ".join(o.rule_id.split("_")[0] for o in r.rule_overrides),
            ]
        if show_route:
            row.append(SHORT[r.route])
        table.add_row(*row)
    return table


def print_summary(console: Console, s: dict) -> None:
    n = s["cases"]
    for line in (
        f"Schema-valid [bold]{s['schema_valid']}/{n}[/] · Category {s['category_correct']}/{n} · "
        f"Priority {s['priority_correct']}/{n} · Queue {s['queue_correct']}/{n}",
        f"P1 recall {s['p1_caught']}/{s['p1_total']} · P1 precision {s['p1_called_correctly']}/{s['p1_called']} · "
        f"Injection flagged {s['injection_flagged']}/{s['injection_total']}",
        f"Rule overrides {s['rule_overrides']} · Human-Review {s['human_review']} · Revisions {s['revisions']}",
        f"Cost ${s['cost_usd']:.4f} (${s['cost_per_case_usd']:.4f}/case) · "
        f"Latency p50 {s['latency_p50_s']}s, p95 {s['latency_p95_s']}s",
        f"Tokens in {s['input_tokens']:,} · out {s['output_tokens']:,} · cache reads {s['cache_read_tokens']:,}",
    ):
        console.print(line)
    c = s["challenger"]
    if c["challenged"]:
        wrong, right = c["catch"] + c["miss"], c["false_alarm"] + c["agreed_right"]
        console.print(
            f"Challenger, routing: caught {c['catch']} of {wrong} wrong first passes · "
            f"false alarms on {c['false_alarm']} of {right} right ones · revision fixed {c['fixed']}, broke {c['broke']}"
        )
        console.print(f"Challenger, drafts: objected to {c['draft_objections']} · triage accepted {c['draft_accepted']}")


def print_drafts(console: Console, results: list[tuple[LabeledCase, PipelineResult]]) -> None:
    for labeled, r in results:
        if r.final:
            console.print(f"\n[bold]{labeled.case.case_id}[/] [dim]{r.final.rationale}[/]")
            console.print(r.final.draft_first_response or "[italic](no reply)[/]")


def header(settings: Settings, pipeline: Pipeline, n: int, max_usd: float) -> str:
    spec = settings.pipelines[pipeline.name]
    triage = settings.agents["triage"]
    models = f"triage {spec.triage_model or triage.model} ({triage.effort})"
    if pipeline.challenger:
        challenger = settings.agents["challenger"]
        models += f", challenger {spec.challenger_model or challenger.model} ({challenger.effort})"
    return f"[bold]Pipeline {pipeline.name}[/]: {spec.description} · {models} · {n} cases · cap ${max_usd:.2f}"


def load_run(run_dir: Path) -> list[tuple[LabeledCase, PipelineResult]]:
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines() if line.strip()]
    return [(LabeledCase.model_validate(r["label"]), PipelineResult.model_validate(r["result"])) for r in rows]


def rescore(console: Console, run_dir: Path) -> int:
    """Re-scores a saved run with today's metrics. Free: reads results.jsonl and rewrites summary.json."""
    results = load_run(run_dir)
    old = json.loads((run_dir / "summary.json").read_text()) if (run_dir / "summary.json").exists() else {}
    summary = summarize(results) | {k: old[k] for k in ("pipeline", "cases_file", "not_started", "stopped_by") if k in old}
    summary["rescored_at"] = datetime.now().isoformat(timespec="seconds")
    console.print(f"[bold]Re-scored[/] {run_dir.name} ({len(results)} cases, no API calls)")
    console.print(results_table(results, show_route=any(r.trace and r.route != Route.NO_CHALLENGER for _, r in results)))
    print_summary(console, summary)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=ROOT / "data" / "cases_smoke.jsonl")
    parser.add_argument("--pipeline", default="A", help="a key under pipelines: in config.yaml")
    parser.add_argument("--offset", type=int, default=0, help="skip the first N cases (e.g. ones already run)")
    parser.add_argument("--limit", type=int, help="run only the first N cases (after --offset)")
    parser.add_argument("--max-usd", type=float, help="spend cap for this run (default: budget.ceiling_usd)")
    parser.add_argument("--drafts", action="store_true", help="print each draft first response after the table")
    parser.add_argument("--rescore", type=Path, metavar="RUN_DIR", help="re-score a saved run with the current metrics; no API calls")
    args = parser.parse_args(argv)
    console = Console()

    if args.rescore:
        return rescore(console, args.rescore)

    load_dotenv(ROOT / ".env")
    if reason := preflight(os.environ):
        console.print(f"[red]{reason}[/]")
        return 2

    settings = load_settings()
    if args.pipeline not in settings.pipelines:
        console.print(f"[red]No pipeline {args.pipeline!r} in config.yaml. Options: {', '.join(settings.pipelines)}[/]")
        return 2
    max_usd = args.max_usd if args.max_usd is not None else settings.budget_ceiling_usd
    if max_usd is None:
        console.print("[red]Set --max-usd or budget.ceiling_usd in config.yaml.[/]")
        return 2

    spec = settings.pipelines[args.pipeline]
    pipeline = Pipeline.from_settings(args.pipeline, settings)
    agents = ClaudeAgents(settings, triage_model=spec.triage_model, challenger_model=spec.challenger_model)
    cases = load_cases(args.cases)[args.offset :][: args.limit]

    out_dir = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{pipeline.name}-{args.cases.stem}"
    out_dir.mkdir(parents=True)
    console.print(header(settings, pipeline, len(cases), max_usd))

    def progress(labeled: LabeledCase, r: PipelineResult) -> None:
        got = f"{r.final.category.value} · {r.final.priority.value}" if r.final else f"ERROR {r.error}"
        console.print(f"  {labeled.case.case_id}  {got}  [dim]${r.cost_usd:.4f}  {r.latency_s:.1f}s[/]")

    outcome = run_cases(cases, agents, pipeline, max_usd, out_dir / "results.jsonl", progress)
    console.print(results_table(outcome.results, show_route=pipeline.challenger))
    summary = summarize(outcome.results) | {
        "pipeline": pipeline.name,
        "cases_file": str(args.cases),
        "not_started": outcome.not_started,
        "stopped_by": outcome.stopped_by,
    }
    print_summary(console, summary)
    if args.drafts:
        print_drafts(console, outcome.results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    if outcome.stopped_by:
        console.print(f"\n[bold red]Stopped early:[/] {outcome.stopped_by} ({outcome.not_started} cases not started)")
    console.print(f"\n[dim]Saved to {out_dir}[/]")
    return 1 if outcome.stopped_by else 0


if __name__ == "__main__":
    sys.exit(main())
