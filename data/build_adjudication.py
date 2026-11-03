"""Builds data/adjudicate.html: the gold cases every pipeline missed, for Aneesh to adjudicate.

    python -m data.build_adjudication [--run eval/runs/<stamp>-eval]

Each case shows his label, the generator's label, what the pipelines said, the full system's
reasoning, and Claude's guess at why they differ. He marks each one "mine stands", "system's
right" (with the corrected label), or "both defensible", and can write down the rules he
applied. This is adjudication, not blind labeling: the first-contact numbers stay in git.
Answers save in the browser (localStorage key triage-bench-adjudication).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

from eval.run_eval import GOLD, with_gold
from triage.run import RUNS_DIR, load_cases, load_run
from triage.schemas import Category, LabeledCase, PipelineResult, Priority

from .build_label_tool import TAXONOMY, section_items
from .spec import DATA_DIR, PROPERTIES

TEMPLATE = DATA_DIR / "adjudicate.template.html"
OUT = DATA_DIR / "adjudicate.html"
GUESSES = DATA_DIR / "adjudication_guesses.yaml"
FULL_SYSTEM = "D"
GROUPS = [
    ("more_urgent", "You rated it more urgent than the pipelines"),
    ("less_urgent", "You rated it less urgent than the pipelines"),
    ("category", "Different category, same priority"),
    ("both", "Category and priority both differ"),
]
RANK = {p.value: i for i, p in enumerate(Priority)}


def _pair(category: str, priority: str) -> dict:
    return {"category": category, "priority": priority}


def missed_by_all(runs: dict[str, dict[str, PipelineResult]], gold: dict[str, LabeledCase]) -> list[str]:
    def right(cid: str, r: PipelineResult) -> bool:
        accepted = {(g.category, g.priority) for g in [gold[cid].label, *gold[cid].also_accept]}
        return r.final is not None and (r.final.category, r.final.priority) in accepted

    return sorted(cid for cid in gold if all(cid in rs and not right(cid, rs[cid]) for rs in runs.values()))


def group_of(gold: dict, said: dict) -> str:
    same_cat = gold["category"] == said["category"]
    if gold["priority"] == said["priority"]:
        return "category"
    if not same_cat:
        return "both"
    return "more_urgent" if RANK[gold["priority"]] < RANK[said["priority"]] else "less_urgent"


def page_data(run_dir: Path, gold: dict[str, LabeledCase], guesses: dict[str, str]) -> dict:
    names = sorted(p.name for p in run_dir.iterdir() if (p / "results.jsonl").exists())
    runs = {n: {lc.case.case_id: r for lc, r in with_gold(load_run(run_dir / n), gold)} for n in names}
    cases = []
    for cid in missed_by_all(runs, gold):
        lc = gold[cid]
        answers = {n: f"{rs[cid].final.category.value} / {rs[cid].final.priority.value}" for n, rs in runs.items() if rs[cid].final}
        top, count = Counter(answers.values()).most_common(1)[0]
        said = _pair(*top.split(" / "))
        mine = _pair(lc.label.category.value, lc.label.priority.value)
        blind = lc.review.blind_label if lc.review else None
        full = runs.get(FULL_SYSTEM, {}).get(cid)
        cases.append({
            "id": cid,
            "channel": lc.case.channel.value,
            "received": f"{lc.case.received_at:%a %b %-d · %-I:%M %p}",
            "role": lc.case.contact_role.value,
            "property": PROPERTIES.get(lc.case.property_id or "", lc.case.property_id or "no property"),
            "subject": lc.case.subject,
            "body": lc.case.body,
            "language": (lc.brief or {}).get("language", "English"),
            "hard": lc.hard_case_tags,
            "mine": mine | {"flags": [f.value for f in lc.label.escalation_flags]},
            "blind": _pair(blind.category.value, blind.priority.value)
            if blind and (blind.category, blind.priority) != (lc.label.category, lc.label.priority) else None,
            "generator": _pair(lc.review.spec_label.category.value, lc.review.spec_label.priority.value) if lc.review else None,
            "said": said | {"count": count, "of": len(answers)},
            "answers": answers,
            "reasoning": full.final.rationale if full and full.final else "",
            "reasoning_from": FULL_SYSTEM if full else "",
            "notes": lc.review.notes if lc.review else "",
            "guess": guesses.get(cid, ""),
            "group": group_of(mine, said),
        })
    md = TAXONOMY.read_text()
    return {
        "run": run_dir.name,
        "pipelines": names,
        "groups": [{"key": k, "title": t} for k, t in GROUPS],
        "categories": [c.value for c in Category],
        "priorities": [p.value for p in Priority],
        "category_help": section_items(md, "Categories"),
        "priority_help": section_items(md, "Priorities"),
        "cases": cases,
    }


def build(data: dict, head_extra: str = "", footer: str = "") -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (
        TEMPLATE.read_text()
        .replace("__TB_DATA__", payload)
        .replace("<!--HEAD_EXTRA-->", head_extra)
        .replace("<!--FOOTER-->", footer)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=Path, help="eval run folder (default: the newest *-eval in eval/runs)")
    args = parser.parse_args(argv)
    run_dir = args.run or max(RUNS_DIR.glob("*-eval"), default=None)
    if run_dir is None:
        print("No eval run yet. Run python -m eval.run_eval first.")
        return 2
    gold = {lc.case.case_id: lc for lc in load_cases(GOLD)}
    guesses = yaml.safe_load(GUESSES.read_text()) if GUESSES.exists() else {}
    data = page_data(run_dir, gold, guesses)
    OUT.write_text(build(data))
    print(f"Wrote {OUT.relative_to(DATA_DIR.parent)} with {len(data['cases'])} cases from {run_dir.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
