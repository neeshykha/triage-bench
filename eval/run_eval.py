"""Milestone 5: every pipeline on the gold set, compared in one table.

    python -m eval.run_eval --max-usd 20

Runs each configuration (A-F by default) over data/cases_test.jsonl, Aneesh's 60 blind
gold labels, all pipelines at once so they share the same conditions. Each run is saved
like triage.run saves one. Then it writes eval/results.md and eval/results.json: accuracy
with 95% ranges (60 cases is small, so a two-case gap can be noise), confusion, P1 recall
and precision, challenger catches and false alarms, Human-Review rate, injection
resistance, cost, and latency. Prompts were tuned on the dev set only; this is the first
time any of them meets the gold labels.

Scoring always reads today's gold labels, so a label fixed after a run needs only
--rescore EVAL_DIR (no API calls). An interrupted eval continues with --resume EVAL_DIR,
running only the cases each pipeline hasn't finished.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from triage.agents import Agents, ClaudeAgents
from triage.config import PipelineSpec, Settings, load_settings
from triage.orchestrator import Pipeline
from triage.run import RUNS_DIR, _percentile, challenger_stats, load_cases, load_run, preflight, run_cases, summarize
from triage.schemas import EscalationFlag, LabeledCase, PipelineResult, Priority, Route, fold_retired

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "data" / "cases_test.jsonl"
PRIORITIES = [p.value for p in Priority]

Results = list[tuple[LabeledCase, PipelineResult]]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% range for a proportion; honest at small n, where k/n alone overstates certainty."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def with_gold(results: Results, gold: dict[str, LabeledCase]) -> Results:
    """Swap each saved label for today's gold label, so fixing a label never means rerunning."""
    return [(gold.get(lc.case.case_id, lc), r) for lc, r in results]


def _got(f, field: str) -> str | None:
    return getattr(f, field).value if f else None


def _accepted(lc: LabeledCase, f) -> LabeledCase:
    """Score against whichever accepted answer the pipeline came closest to ("both defensible" cases have two)."""
    if not lc.also_accept or f is None:
        return lc
    hits = lambda g: (g.category == f.category) + (g.priority == f.priority)
    best = max([lc.label, *lc.also_accept], key=hits)  # ties keep his own label, which comes first
    return lc if best is lc.label else lc.model_copy(update={"label": best})


def score(results: Results) -> dict:
    results = [(_accepted(lc, r.final), r) for lc, r in results]
    n = len(results)
    rank = {p: i for i, p in enumerate(PRIORITIES)}
    finals = [(lc, r.final) for lc, r in results]
    cat = sum(_got(f, "category") == lc.label.category.value for lc, f in finals)
    pri = sum(_got(f, "priority") == lc.label.priority.value for lc, f in finals)
    gold_p1 = [f for lc, f in finals if lc.label.priority == Priority.P1]
    called_p1 = [lc for lc, f in finals if _got(f, "priority") == "P1"]
    caught_p1 = sum(_got(f, "priority") == "P1" for f in gold_p1)
    matrix = {g: {p: 0 for p in PRIORITIES + ["none"]} for g in PRIORITIES}
    for lc, f in finals:
        matrix[lc.label.priority.value][_got(f, "priority") or "none"] += 1
    confused = Counter(
        (lc.label.category.value, _got(f, "category")) for lc, f in finals if _got(f, "category") != lc.label.category.value
    )
    injection = [(lc, f) for lc, f in finals if "injection" in lc.hard_case_tags]
    latencies = [r.latency_s for _, r in results]
    cost = sum(r.cost_usd for _, r in results)
    return {
        "cases": n,
        "both": sum(
            _got(f, "category") == lc.label.category.value and _got(f, "priority") == lc.label.priority.value for lc, f in finals
        ),
        "generator_both": sum(
            _got(f, "category") == lc.review.spec_label.category.value and _got(f, "priority") == lc.review.spec_label.priority.value
            for lc, f in finals if lc.review
        ),
        "category": cat,
        "category_ci": wilson(cat, n),
        "priority": pri,
        "priority_ci": wilson(pri, n),
        "queue": sum(r.queue == lc.label.queue for lc, r in results),
        "p1_recall": [caught_p1, len(gold_p1)],
        "p1_recall_ci": wilson(caught_p1, len(gold_p1)),
        "p1_precision": [sum(lc.label.priority == Priority.P1 for lc in called_p1), len(called_p1)],
        "under_triage": [lc.case.case_id for lc, f in finals if f and rank[f.priority.value] > rank[lc.label.priority.value]],
        "over_triage": [lc.case.case_id for lc, f in finals if f and rank[f.priority.value] < rank[lc.label.priority.value]],
        "priority_matrix": matrix,
        "category_confusions": [{"gold": g, "got": p, "count": k} for (g, p), k in confused.most_common()],
        "human_review": sum(1 for _, r in results if r.route in (Route.UNRESOLVED, Route.AGENT_ERROR)),
        "revisions": sum(r.revisions for _, r in results),
        "rule_overrides": sum(len(r.rule_overrides) for _, r in results),
        "injection": {
            "cases": len(injection),
            "flagged": sum(1 for _, f in injection if f and EscalationFlag.INJECTION_ATTEMPT in f.escalation_flags),
            "unswayed": sum(
                1 for lc, f in injection
                if _got(f, "priority") == lc.label.priority.value and _got(f, "category") == lc.label.category.value
            ),
        },
        "challenger": challenger_stats(results),
        "cost_usd": round(cost, 4),
        "cost_per_case": round(cost / n, 5) if n else 0.0,
        "latency_p50": round(_percentile(latencies, 0.5), 2),
        "latency_p95": round(_percentile(latencies, 0.95), 2),
        "misses": [
            {"case_id": lc.case.case_id, "gold": f"{lc.label.category.value} / {lc.label.priority.value}",
             "got": f"{_got(f, 'category')} / {_got(f, 'priority')}", "tags": lc.hard_case_tags}
            for lc, f in finals
            if _got(f, "category") != lc.label.category.value or _got(f, "priority") != lc.label.priority.value
        ],
    }


def _pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "n/a"


def _ranged(k: int, n: int, lo_hi) -> str:
    return f"{k}/{n}, {100 * k / n:.0f}% ({100 * lo_hi[0]:.0f}-{100 * lo_hi[1]:.0f})" if n else "n/a"


def first_pass_spread(results: dict[str, Results]) -> int:
    """Cases where pipelines running the same triage agent got different first passes: run-to-run noise."""
    passes: dict[str, set] = {}
    for rs in results.values():
        for lc, r in rs:
            step = next((s for s in r.trace if s.stage == "triage"), None)
            if step:
                passes.setdefault(lc.case.case_id, set()).add((step.data["category"], fold_retired(step.data["priority"])))
    return sum(1 for seen in passes.values() if len(seen) > 1)


def render(
    eval_name: str,
    scores: dict[str, dict],
    descriptions: dict[str, str],
    gold: dict[str, LabeledCase],
    noise: tuple[list[str], int] | None = None,
) -> str:
    reviewed = [lc for lc in gold.values() if lc.review]
    spec_cat = sum(lc.review.spec_label.category == lc.label.category for lc in reviewed)
    spec_pri = sum(lc.review.spec_label.priority == lc.label.priority for lc in reviewed)
    lines = [
        "# Results: every pipeline on the gold set",
        "",
        f"{len(gold)} test cases, labeled blind by Aneesh. Eval run `{eval_name}`; prompts were tuned on the dev set only, "
        "and all pipelines ran at the same time, so latency is measured under the same load.",
        f"Ranges are 95% Wilson intervals: with {len(gold)} cases, pipelines whose ranges overlap may not really differ. "
        f"For scale, the label the case generator was asked to write matched Aneesh's on {_pct(spec_cat, len(reviewed))} "
        f"of categories and {_pct(spec_pri, len(reviewed))} of priorities.",
    ]
    adjudicated = [lc for lc in reviewed if lc.review.adjudication]
    if adjudicated:
        changed = sum(lc.label != lc.review.adjudication.first_contact for lc in adjudicated)
        dates = sorted({lc.review.adjudication.decided for lc in adjudicated})
        lines.append(
            f"After the first run, Aneesh adjudicated the {len(adjudicated)} cases every pipeline missed ({', '.join(dates)}), "
            f"seeing what the pipelines said and why: {changed} labels changed, {sum(bool(lc.also_accept) for lc in adjudicated)} "
            f"accept either answer, and {len(adjudicated) - changed - sum(bool(lc.also_accept) for lc in adjudicated)} stand. "
            "Scores here use the adjudicated labels; the first-contact results are in `eval/results-first-contact.md`."
        )
    lines += [
        "",
        "| Pipeline | Category (95% range) | Priority (95% range) | P1 recall (95% range) | P1 precision | Under / over-triaged "
        "| Human-Review | Cost per case | Latency p50 / p95 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, s in scores.items():
        (r, rn), (pk, pn) = s["p1_recall"], s["p1_precision"]
        lines.append(
            f"| **{name}** {descriptions[name]} | {_ranged(s['category'], s['cases'], s['category_ci'])} "
            f"| {_ranged(s['priority'], s['cases'], s['priority_ci'])} | {_ranged(r, rn, s['p1_recall_ci'])} "
            f"| {pk}/{pn} | {len(s['under_triage'])} / {len(s['over_triage'])} | {s['human_review']} "
            f"| ${s['cost_per_case']:.4f} | {s['latency_p50']}s / {s['latency_p95']}s |"
        )
    if noise and len(noise[0]) > 1:
        lines += [
            "",
            f"Pipelines {', '.join(noise[0])} run the same triage agent, and their first passes differed on {noise[1]} of "
            f"{len(gold)} cases from run-to-run variation alone, so gaps of a few cases between them are noise.",
        ]

    lines += [
        "", "## Against the generator's label", "",
        "Each case was written from a scenario whose label follows the written taxonomy. Comparing each pipeline with that "
        "label and with Aneesh's shows whether misses come from the models or from a gap between the taxonomy and his judgment.",
        "", "| Pipeline | Category and priority both right, vs Aneesh | vs the generator's label | Rule overrides |", "|---|---|---|---|",
    ]
    for name, s in scores.items():
        lines.append(f"| {name} | {_pct(s['both'], s['cases'])} | {_pct(s['generator_both'], s['cases'])} | {s['rule_overrides']} |")

    challenged ={n: s["challenger"] for n, s in scores.items() if s["challenger"]["challenged"]}
    if challenged:
        lines += [
            "", "## The challenger", "",
            "Routing objections scored against the gold label of the first pass it saw. Draft objections have no label, so they're counted apart.",
            "", "| Pipeline | Caught wrong first passes | False alarms on right ones | After revision: fixed / broke | Draft objections (accepted) |",
            "|---|---|---|---|---|",
        ]
        for name, c in challenged.items():
            lines.append(
                f"| {name} | {c['catch']} of {c['catch'] + c['miss']} | {c['false_alarm']} of {c['false_alarm'] + c['agreed_right']} "
                f"| {c['fixed']} / {c['broke']} | {c['draft_objections']} ({c['draft_accepted']}) |"
            )

    lines += ["", "## Injection resistance", "", "| Pipeline | Flagged | Not swayed (right category and priority) |", "|---|---|---|"]
    for name, s in scores.items():
        i = s["injection"]
        lines.append(f"| {name} | {i['flagged']} of {i['cases']} | {i['unswayed']} of {i['cases']} |")

    lines += ["", "## Priority confusion", "", "Rows are the gold label, columns what the pipeline said.", ""]
    for name, s in scores.items():
        lines += [f"**{name}**", "", "| gold \\ said | " + " | ".join(PRIORITIES) + " | error |", "|---" * 6 + "|"]
        lines += [f"| {g} | " + " | ".join(str(row[p]) for p in PRIORITIES + ["none"]) + " |" for g, row in s["priority_matrix"].items()]
        lines.append("")

    lines += ["## Most-confused categories", ""]
    for name, s in scores.items():
        top = ", ".join(f"{c['gold']} as {c['got']} ({c['count']})" for c in s["category_confusions"][:3]) or "none"
        lines.append(f"- **{name}**: {top}")

    missed_by_all = set.intersection(*({m["case_id"] for m in s["misses"]} for s in scores.values())) if scores else set()
    lines += ["", "## Missed by every pipeline", ""]
    answers = {cid: Counter(m["got"] for s in scores.values() for m in s["misses"] if m["case_id"] == cid) for cid in missed_by_all}
    said = {cid: c.most_common(1)[0][0] for cid, c in answers.items()}
    lines += [
        f"- {cid}: gold {gold[cid].label.category.value} / {gold[cid].label.priority.value}; "
        f"{answers[cid][said[cid]]} of {len(scores)} said {said[cid]}"
        + (" (the generator's label)" if gold[cid].review and said[cid] == f"{gold[cid].review.spec_label.category.value} / {gold[cid].review.spec_label.priority.value}" else "")
        + (f" [{', '.join(gold[cid].hard_case_tags)}]" if gold[cid].hard_case_tags else "")
        for cid in sorted(missed_by_all) if cid in gold
    ] or ["None."]
    lines += ["", "Every miss, per pipeline, is in `eval/results.json`."]
    return "\n".join(lines) + "\n"


def report(
    eval_dir: Path, names: list[str], settings: Settings, gold: dict[str, LabeledCase], out_dir: Path = ROOT / "eval"
) -> str:
    scores, runs = {}, {}
    for name in names:
        runs[name] = results = with_gold(load_run(eval_dir / name), gold)
        scores[name] = score(results)
        (eval_dir / name / "summary.json").write_text(
            json.dumps(summarize(results) | {"pipeline": name, "cases_file": str(GOLD)}, indent=2) + "\n"
        )
    default = settings.agents["triage"].model
    same_agent = [n for n in names if (settings.pipelines[n].triage_model or default) == default]
    noise = (same_agent, first_pass_spread({n: runs[n] for n in same_agent}))
    text = render(eval_dir.name, scores, {n: settings.pipelines[n].description for n in names}, gold, noise)
    (out_dir / "results.md").write_text(text)
    (out_dir / "results.json").write_text(json.dumps({"eval_run": eval_dir.name, "pipelines": scores}, indent=2) + "\n")
    return text


def finished(run_dir: Path) -> set[str]:
    return {lc.case.case_id for lc, _ in load_run(run_dir)} if (run_dir / "results.jsonl").exists() else set()


def run_all(
    names: list[str],
    cases: list[LabeledCase],
    eval_dir: Path,
    cap: float,
    settings: Settings,
    make_agents: Callable[[PipelineSpec], Agents],
    log: Callable[[str], None],
) -> dict[str, str]:
    """Runs every pipeline's unfinished cases at once; returns why any pipeline stopped early."""
    spent = sum(r.cost_usd for n in names if (eval_dir / n / "results.jsonl").exists() for _, r in load_run(eval_dir / n))
    todo = [n for n in names if {c.case.case_id for c in cases} - finished(eval_dir / n)]
    if not todo:
        return {}
    share = max(0.0, cap - spent) / len(todo)  # each pipeline's slice keeps the total under the cap
    log(f"{len(todo)} pipeline(s) to run · ${spent:.2f} already spent · up to ${share:.2f} each")

    def one(name: str) -> str | None:
        run_dir = eval_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        done = finished(run_dir)
        spec = settings.pipelines[name]

        def progress(lc: LabeledCase, r: PipelineResult) -> None:
            got = f"{r.final.category.value} / {r.final.priority.value}" if r.final else f"ERROR {r.error}"
            log(f"  {name}  {lc.case.case_id}  {got}  ${r.cost_usd:.4f}  {r.latency_s:.1f}s")

        outcome = run_cases(
            [c for c in cases if c.case.case_id not in done],
            make_agents(spec), Pipeline.from_settings(name, settings), share, run_dir / "results.jsonl", progress,
        )
        log(f"{name} {'stopped: ' + outcome.stopped_by if outcome.stopped_by else 'done'} · ${outcome.spent:.4f} this run")
        return outcome.stopped_by

    with ThreadPoolExecutor(max_workers=len(todo)) as pool:
        stops = dict(zip(todo, pool.map(one, todo)))
    return {n: why for n, why in stops.items() if why}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipelines", default="A,B,C,D,E,F", help="comma-separated keys under pipelines: in config.yaml")
    parser.add_argument("--max-usd", type=float, help="cap for the whole eval (default: budget.ceiling_usd)")
    parser.add_argument("--resume", type=Path, metavar="EVAL_DIR", help="continue an eval that stopped early")
    parser.add_argument("--rescore", type=Path, metavar="EVAL_DIR", help="rebuild the results from saved runs; no API calls")
    args = parser.parse_args(argv)
    console = Console(highlight=False)
    settings = load_settings()
    names = [n.strip() for n in args.pipelines.split(",") if n.strip()]
    if unknown := [n for n in names if n not in settings.pipelines]:
        console.print(f"[red]No pipeline {', '.join(unknown)} in config.yaml.[/]")
        return 2
    gold = {lc.case.case_id: lc for lc in load_cases(GOLD)}

    if args.rescore:
        present = [n for n in names if (args.rescore / n / "results.jsonl").exists()]
        print(report(args.rescore, present, settings, gold))
        return 0

    load_dotenv(ROOT / ".env")
    if reason := preflight(os.environ):
        console.print(f"[red]{reason}[/]")
        return 2
    if any(lc.label_source != "human" for lc in gold.values()):
        console.print("[red]The gold set isn't fully human-labeled yet. Merge Aneesh's labels first.[/]")
        return 2
    cap = args.max_usd if args.max_usd is not None else settings.budget_ceiling_usd
    eval_dir = args.resume or RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-eval"
    console.print(f"[bold]Gold-set eval[/]: {', '.join(names)} · {len(gold)} cases · cap ${cap:.2f} · {eval_dir}")

    stops = run_all(
        names, list(gold.values()), eval_dir, cap, settings,
        lambda spec: ClaudeAgents(settings, triage_model=spec.triage_model, challenger_model=spec.challenger_model),
        lambda line: console.print(line, markup=False),
    )
    if stops:
        for name, why in stops.items():
            console.print(f"[bold red]{name} stopped early:[/] {why}")
        console.print(f"Continue with: python -m eval.run_eval --resume {eval_dir} --max-usd {cap}")
        return 1
    print(report(eval_dir, names, settings, gold))
    total = sum(r.cost_usd for n in names for _, r in load_run(eval_dir / n))
    console.print(f"[dim]Total ${total:.4f}. Runs in {eval_dir}; results in eval/results.md and eval/results.json.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
