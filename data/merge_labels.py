"""Folds hand labels from the labeling page into data/cases_test.jsonl, making it the gold set.

    python -m data.merge_labels gold_labels.json

Takes the JSON from the page's "Download labels" or "Copy for Claude". Each labeled
case gets its final label, label_source "human", and a review record holding the
generator's original label and the blind first pick, so the write-up can report how
often the generator was wrong and how often the reveal changed a mind. The old file is
copied to cases_test.jsonl.bak first.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from triage.schemas import GoldLabel, LabeledCase, LabelReview

from .spec import queue_for
from .split import TEST, read_cases, write_cases

FIELDS = ("category", "priority", "queue", "escalation_flags")


def _same(a: GoldLabel, b: GoldLabel, field: str) -> bool:
    x, y = getattr(a, field), getattr(b, field)
    return sorted(x) == sorted(y) if isinstance(x, list) else x == y


def merge(cases: list[LabeledCase], labels: dict[str, dict]) -> tuple[list[LabeledCase], list[str]]:
    """Returns the updated cases and any problems found. Cases without a label are left as they were."""
    problems, merged = [], []
    for c in cases:
        raw = labels.get(c.case.case_id)
        if raw is None:
            merged.append(c)
            continue
        try:
            final = GoldLabel.model_validate(raw["final"])
            blind = GoldLabel.model_validate(raw["blind"]) if raw.get("blind") else None
        except (KeyError, ValidationError) as err:
            problems.append(f"{c.case.case_id}: {err}")
            merged.append(c)
            continue
        spec = c.review.spec_label if c.review else c.label
        merged.append(
            c.model_copy(
                update={
                    "label": final,
                    "label_source": "human",
                    "review": LabelReview(spec_label=spec, blind_label=blind, notes=raw.get("notes", "")),
                }
            )
        )
    unknown = sorted(set(labels) - {c.case.case_id for c in cases})
    problems += [f"{case_id}: not in the test set" for case_id in unknown]
    return merged, problems


def agreement(cases: list[LabeledCase]) -> dict[str, dict[str, int]]:
    """Per field: how often the generator's label matched the blind pick and the final label,
    and how often the reveal changed the answer."""
    reviewed = [c for c in cases if c.review and c.review.blind_label]
    out = {}
    for field in FIELDS:
        out[field] = {
            "reviewed": len(reviewed),
            "generator_matched_blind": sum(_same(c.review.spec_label, c.review.blind_label, field) for c in reviewed),
            "generator_matched_final": sum(_same(c.review.spec_label, c.label, field) for c in reviewed),
            "changed_after_reveal": sum(not _same(c.review.blind_label, c.label, field) for c in reviewed),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("labels", type=Path, help="JSON from the labeling page")
    parser.add_argument("--allow-partial", action="store_true", help="merge even if some gold cases are unlabeled")
    args = parser.parse_args(argv)

    payload = json.loads(args.labels.read_text())
    labels = payload.get("labels", payload)
    cases = read_cases(TEST)
    merged, problems = merge(cases, labels)
    for p in problems:
        print(f"problem: {p}")
    missing = [c.case.case_id for c in merged if c.label_source != "human"]
    if problems:
        print("Nothing written.")
        return 2
    if missing and not args.allow_partial:
        print(f"{len(missing)} of {len(cases)} gold cases have no label yet ({', '.join(missing[:8])}...). "
              "Finish them, or pass --allow-partial. Nothing written.")
        return 2

    drift = [c.case.case_id for c in merged if c.label_source == "human" and c.label.queue != queue_for(c.label.category, c.label.escalation_flags)]
    shutil.copy2(TEST, TEST.with_suffix(".jsonl.bak"))
    write_cases(TEST, merged)
    print(f"Merged {len(merged) - len(missing)} human labels into {TEST.name} (backup: {TEST.name}.bak).")
    for field, s in agreement(merged).items():
        print(
            f"  {field:17} generator matched your blind pick {s['generator_matched_blind']}/{s['reviewed']}, "
            f"your final {s['generator_matched_final']}/{s['reviewed']}; reveal changed {s['changed_after_reveal']}"
        )
    if drift:
        print(f"  queue overridden from the standard mapping on: {', '.join(drift)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
