"""Replays one saved case, stage by stage, from a run's trace. Free: no API calls.

    python -m triage.replay eval/runs/<run>/F NG-194 --pace 6
    python -m triage.replay eval/runs/<run>/F NG-194 --frames docs/demo-frames

Shows what each stage saw and decided: the case, the rules precheck, the triage agent's
call, any rule override, the challenger's objections, the revision, the re-check, and where
the case ended up, against the gold label. --pace waits between stages for a live screen
recording; --frames writes each stage as a same-sized SVG instead, for building a GIF.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .run import load_run
from .schemas import LabeledCase, PipelineResult, Route

STAGES = ["case", "precheck", "triage", "rules", "challenge", "revise", "recheck", "route"]
LABELS = {"case": "case", "precheck": "rules precheck", "triage": "triage", "rules": "rules", "challenge": "challenger",
          "revise": "revise", "recheck": "re-check", "route": "route"}
WIDTH = 100
ROUTES = {
    Route.NO_CHALLENGER: "no challenger in this pipeline",
    Route.AGREED: "the challenger agreed on the first pass",
    Route.AGREED_AFTER_REVISION: "the challenger agreed after one revision",
    Route.UNRESOLVED: "still disputed after one revision, so it goes to Human-Review",
    Route.AGENT_ERROR: "an agent failed, so it goes to Human-Review",
}


def flow(current: str, ran: set[str]) -> Text:
    """The pipeline as one line: the current stage highlighted, stages this pipeline skips struck through."""
    out = Text()
    for i, stage in enumerate(STAGES):
        style = "bold #111111 on #7dd3fc" if stage == current else ("white" if stage in ran else "dim strike")
        out.append(f" {LABELS[stage]} ", style=style)
        if i < len(STAGES) - 1:
            out.append(" → ", style="dim")
    return out


def _decision(d: dict) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", no_wrap=True)
    t.add_column()
    for key, label in (("category", "Category"), ("priority", "Priority"), ("queue", "Queue")):
        t.add_row(label, Text(str(d[key]), style="bold"))
    t.add_row("Flags", ", ".join(d["escalation_flags"]) or "none")
    if "confidence" in d:
        t.add_row("Confidence", f"{d['confidence']:.2f}")
    return t


def _cost(r: PipelineResult, stage: str) -> str:
    u = next((u for u in r.usage if u.stage == stage), None)
    return f"{u.model} · ${u.cost_usd:.4f} · {u.latency_s:.1f}s" if u else ""


def frames(lc: LabeledCase, r: PipelineResult, pipeline: str) -> list[tuple[str, Panel]]:
    steps = {s.stage: s.data for s in r.trace}
    ran = {"case", "route"} | set(steps) | ({"rules"} if "precheck" in steps else set())
    out: list[tuple[str, Panel]] = []
    c = lc.case
    meta = f"{c.channel.value} · {c.received_at:%a %b %-d, %-I:%M %p} · from a {c.contact_role.value.lower()}"
    out.append(("case", Panel(Group(Text(meta, style="dim"), Text(""), Text(c.subject or "(no subject)", style="bold"), Text(c.body)),
                              title=f"{c.case_id} arrives", title_align="left")))

    if "precheck" in steps:
        signals = [f"{k.replace('_', ' ')}: {v}" for k, v in steps["precheck"].items() if v]
        body = Text("Deterministic checks before any model runs. " + ("Found " + "; ".join(signals) if signals else "Nothing found."))
    else:
        body = Text(f"Pipeline {pipeline} runs without the rules layer, so there's no precheck and no rule can override the model.", style="dim")
    out.append(("precheck", Panel(body, title="Rules precheck", title_align="left")))

    t = steps["triage"]
    out.append(("triage", Panel(Group(_decision(t), Text(""), Text("Why: ", style="dim") + Text(t["rationale"])),
                                title="Triage agent", subtitle=_cost(r, "triage"), title_align="left", subtitle_align="right")))

    overrides = [o for o in r.rule_overrides if o.stage == "triage"]
    if overrides:
        body = Group(*[Text(f"{o.rule_id}: {o.field} {o.before} → {o.after} ({o.reason})", style="bold") for o in overrides])
    elif "precheck" in steps:
        body = Text("No rule fired. Rules only ever raise urgency, so they had nothing to add.")
    else:
        body = Text("Rules are off in this pipeline.", style="dim")
    out.append(("rules", Panel(body, title="Rules after triage", title_align="left")))

    if "challenge" in steps:
        ch = steps["challenge"]
        parts = [Text("Sees the case and the decision, never the triage agent's reasoning or confidence.", style="dim"), Text("")]
        if ch["verdict"] == "agree":
            parts.append(Text("Agrees.", style="bold green"))
        for o in ch["objections"]:
            parts += [Text(f"Objects to {o['field']}" + (f": suggests {o['suggested_value']}" if o.get("suggested_value") else ""), style="bold yellow"),
                      Text(o["problem"]), Text("")]
        parts = parts[:-1] if ch["objections"] else parts
        out.append(("challenge", Panel(Group(*parts), title="Challenger", subtitle=_cost(r, "challenge"), title_align="left", subtitle_align="right")))

    if "revise" in steps:
        rv = steps["revise"]
        parts = []
        for x in rv["responses"]:
            parts += [Text(f"{x['decision'].capitalize()}s the {x['field']} objection", style="bold"), Text(x["reason"]), Text("")]
        before, after = steps["triage"], rv["revised"]
        changed = [f"{k}: {before[k]} → {after[k]}" for k in ("category", "priority", "queue") if before[k] != after[k]]
        parts.append(Text("Changed: " + ("; ".join(changed) if changed else "routing unchanged (draft reply only)"), style="bold cyan"))
        out.append(("revise", Panel(Group(*parts), title="Triage revises, once", subtitle=_cost(r, "revise"), title_align="left", subtitle_align="right")))

    if "recheck" in steps:
        rc = steps["recheck"]
        body = Text("Agrees with the revision.", style="bold green") if rc["verdict"] == "agree" else Group(
            *[Text(f"Still objects to {o['field']}: {o['problem']}", style="bold yellow") for o in rc["objections"]])
        out.append(("recheck", Panel(body, title="Challenger re-checks", subtitle=_cost(r, "recheck"), title_align="left", subtitle_align="right")))

    g, f = lc.label, r.final
    score = Table.grid(padding=(0, 2))
    for col in range(4):
        score.add_column(style="dim" if col == 0 else None)
    score.add_row("", Text("System", style="dim"), Text("Gold label", style="dim"), "")
    rows = [(label, getattr(f, key).value if f else "-", getattr(g, key).value) for key, label in
            (("category", "Category"), ("priority", "Priority"), ("queue", "Queue"))]
    rows.append(("Flags", ", ".join(x.value for x in f.escalation_flags) or "none" if f else "-",
                 ", ".join(x.value for x in g.escalation_flags) or "none"))
    for label, mine, gold in rows:
        score.add_row(label, Text(mine, style="bold"), gold, Text("✓", style="green") if mine == gold else Text("✗", style="red"))
    total = sum(u.cost_usd for u in r.usage)
    tail = Text(f"\nRoute: {ROUTES[r.route]}. Total ${total:.4f}, {r.latency_s:.1f}s across {len(r.usage)} model calls.")
    out.append(("route", Panel(Group(score, tail), title="Where it lands", title_align="left")))
    return [(stage, panel) for stage, panel in out if stage in ran]  # skipped stages show struck through in the flow line


def render(console: Console, stage: str, panel: Panel, ran: set[str], heading: str) -> None:
    console.print(Text(heading, style="bold"))
    console.print(flow(stage, ran))
    console.print()
    console.print(panel)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path, help="a saved run folder (one with results.jsonl)")
    parser.add_argument("case_id")
    parser.add_argument("--pace", type=float, default=0, help="seconds to wait between stages, for a live recording")
    parser.add_argument("--frames", type=Path, help="write one SVG per stage here instead of printing")
    args = parser.parse_args(argv)
    found = next(((lc, r) for lc, r in load_run(args.run_dir) if lc.case.case_id == args.case_id), None)
    if found is None:
        print(f"{args.case_id} isn't in {args.run_dir}.")
        return 2
    lc, r = found
    pipeline = r.pipeline
    ran = {"case", "route"} | {s.stage for s in r.trace} | ({"rules"} if any(s.stage == "precheck" for s in r.trace) else set())
    heading = f"triage-bench · pipeline {pipeline} · one case, stage by stage"
    steps = frames(lc, r, pipeline)

    if args.frames:
        args.frames.mkdir(parents=True, exist_ok=True)
        consoles = []
        for stage, panel in steps:
            console = Console(record=True, width=WIDTH, force_terminal=True, color_system="truecolor", file=io.StringIO())
            render(console, stage, panel, ran, heading)
            consoles.append((stage, console, len(console.export_text(clear=False).splitlines())))
        tallest = max(used for _, _, used in consoles)
        for i, (stage, console, used) in enumerate(consoles):  # pad every frame to the tallest so a GIF doesn't jump
            for _ in range(tallest - used):
                console.print(" ")  # a space, not an empty line: the SVG export drops trailing blank lines
            (args.frames / f"{i:02d}-{stage}.svg").write_text(console.export_svg(title=f"{lc.case.case_id} · {LABELS[stage]}"))
        print(f"Wrote {len(steps)} frames to {args.frames}")
        return 0

    console = Console(width=WIDTH)
    for i, (stage, panel) in enumerate(steps):
        if i and args.pace:
            time.sleep(args.pace)
            console.clear()
        render(console, stage, panel, ran, heading)
    return 0


if __name__ == "__main__":
    sys.exit(main())
