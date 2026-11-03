"""Folds Aneesh's adjudication answers into the gold set.

    python -m data.merge_adjudication data/adjudication_answers.json [more.json ...] [--run EVAL_DIR]

Takes the answers from the adjudication page (later files override earlier ones, case by
case). "mine" keeps his label; "system" replaces it with the corrected label; "both" keeps
his label and adds the other answer to also_accept, so either counts as right. Every
adjudicated case records the label the first eval scored against, so the first-contact
numbers can always be rebuilt. Flags for a new label come from the pipelines that gave that
answer; the queue follows from category and flags. Re-running starts from those recorded
first-contact labels, so it is safe to repeat. The old file is copied to cases_test.jsonl.bak.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from triage.run import RUNS_DIR, load_run
from triage.schemas import (
    Adjudication, Category, EscalationFlag, GoldLabel, LabeledCase, PipelineResult, Priority, fold_retired,
)

from .spec import queue_for
from .split import TEST, read_cases, write_cases


def _flags_for(category: Category, priority: Priority, results: list[PipelineResult], fallback: list[EscalationFlag]):
    """The flags most pipelines added when they gave this exact answer; the old flags if none did."""
    seen = Counter(
        tuple(sorted(r.final.escalation_flags))
        for r in results if r.final and (r.final.category, r.final.priority) == (category, priority)
    )
    return list(seen.most_common(1)[0][0]) if seen else list(fallback)


def adjudicate(
    cases: list[LabeledCase], answers: dict[str, dict], results: dict[str, list[PipelineResult]], decided: str
) -> tuple[list[LabeledCase], list[str]]:
    problems, out = [], []
    known = {c.case.case_id for c in cases}
    problems += [f"{cid}: not in the gold set" for cid in answers if cid not in known]
    for c in cases:
        a = answers.get(c.case.case_id)
        if a is None:
            out.append(c)
            continue
        if c.review is None:
            problems.append(f"{c.case.case_id}: has no review record; merge the gold labels first")
            out.append(c)
            continue
        base = c.review.adjudication.first_contact if c.review.adjudication else c.label
        label, also = base, []
        if a["verdict"] in ("system", "both"):
            try:
                cat, pri = Category(a["category"]), Priority(fold_retired(a["priority"]))
            except (KeyError, ValueError) as err:
                problems.append(f"{c.case.case_id}: bad corrected label ({err})")
                out.append(c)
                continue
            flags = _flags_for(cat, pri, results.get(c.case.case_id, []), base.escalation_flags)
            answer = GoldLabel(category=cat, priority=pri, queue=queue_for(cat, flags), escalation_flags=flags)
            label, also = (answer, []) if a["verdict"] == "system" else (base, [answer] if answer != base else [])
        elif a["verdict"] != "mine":
            problems.append(f"{c.case.case_id}: unknown verdict {a['verdict']!r}")
            out.append(c)
            continue
        record = Adjudication(verdict=a["verdict"], why=a.get("why", ""), first_contact=base, decided=decided)
        out.append(c.model_copy(update={
            "label": label, "also_accept": also, "review": c.review.model_copy(update={"adjudication": record}),
        }))
    return out, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("answers", type=Path, nargs="+", help="answer files from the adjudication page; later ones win")
    parser.add_argument("--run", type=Path, help="eval run the page was built from (default: the newest *-eval)")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args(argv)
    answers: dict[str, dict] = {}
    for path in args.answers:
        answers |= json.loads(path.read_text())["cases"]
    run_dir = args.run or max(RUNS_DIR.glob("*-eval"))
    results: dict[str, list[PipelineResult]] = {}
    for d in sorted(p for p in run_dir.iterdir() if (p / "results.jsonl").exists()):
        for lc, r in load_run(d):
            results.setdefault(lc.case.case_id, []).append(r)

    merged, problems = adjudicate(read_cases(TEST), answers, results, args.date)
    if problems:
        print("Not merged:\n  " + "\n  ".join(problems))
        return 2
    shutil.copy2(TEST, TEST.with_suffix(".jsonl.bak"))
    write_cases(TEST, merged)
    verdicts = Counter(a["verdict"] for a in answers.values())
    changed = sum(1 for c in merged if c.review and c.review.adjudication and c.label != c.review.adjudication.first_contact)
    print(f"Adjudicated {len(answers)} cases ({dict(verdicts)}): {changed} labels changed, "
          f"{sum(1 for c in merged if c.also_accept)} accept a second answer. Backup: {TEST.name}.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
