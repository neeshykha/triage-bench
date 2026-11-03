"""Splits data/cases_all.jsonl into a dev set (iterate freely) and a test set (Aneesh's gold labels).

    python -m data.split

Free and deterministic: the seed in config.yaml fixes which cases land in test, and
data/split.json records the result. It stratifies so every hard-case type and every
priority shows up in test in about its overall share. Human labels already in the test
file are carried over, so regenerating a dev case after labeling is safe; if the test
ids themselves would change, it refuses instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from triage.config import load_settings
from triage.schemas import LabeledCase

from .spec import DATA_DIR, split_ids

ALL = DATA_DIR / "cases_all.jsonl"
DEV = DATA_DIR / "cases_dev.jsonl"
TEST = DATA_DIR / "cases_test.jsonl"
RECORD = DATA_DIR / "split.json"


def read_cases(path: Path) -> list[LabeledCase]:
    return [LabeledCase.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def write_cases(path: Path, cases: list[LabeledCase]) -> None:
    path.write_text("".join(c.model_dump_json() + "\n" for c in sorted(cases, key=lambda c: c.case.case_id)))


def stratum(c: LabeledCase) -> str:
    """Same strata as spec.brief_stratum: the scenario's sampled priority, so relabeling a case
    (by scenario fix or by hand) never moves it between dev and test."""
    if c.hard_case_tags:
        return c.hard_case_tags[0]
    scenario = (c.brief or {}).get("scenario", {})
    return f"ordinary {scenario.get('sampled_as') or scenario.get('priority') or c.label.priority.value}"


def split(cases: list[LabeledCase], test_size: int, seed: int) -> tuple[list[LabeledCase], list[LabeledCase]]:
    test_ids = split_ids([(c.case.case_id, stratum(c)) for c in cases], test_size, seed)
    dev = [c for c in cases if c.case.case_id not in test_ids]
    test = [c for c in cases if c.case.case_id in test_ids]
    return dev, test


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="re-split even if it would discard human labels")
    args = parser.parse_args(argv)

    g = load_settings().generator
    cases = read_cases(ALL)
    if len(cases) != g.cases:
        print(f"Expected {g.cases} cases in {ALL.name}, found {len(cases)}. Finish generating first.")
        return 2

    dev, test = split(cases, g.test_size, g.seed)
    human = {c.case.case_id: c for c in read_cases(TEST) if c.label_source == "human"} if TEST.exists() else {}
    if human and not args.force:
        lost = sorted(set(human) - {c.case.case_id for c in test})
        if lost:
            print(f"Re-splitting would move human-labeled cases out of test ({', '.join(lost[:6])}). "
                  "Nothing written; pass --force if you mean it.")
            return 2
        changed = [cid for cid, c in human.items() if next(t for t in test if t.case.case_id == cid).case != c.case]
        if changed:
            print(f"These human-labeled cases have new text, so their labels no longer apply: {', '.join(changed)}. "
                  "Nothing written; pass --force to drop those labels.")
            return 2
        test = [human.get(c.case.case_id, c) for c in test]  # keep the human labels
    write_cases(DEV, dev)
    write_cases(TEST, test)
    counts = defaultdict(lambda: [0, 0])
    test_ids = {c.case.case_id for c in test}
    for c in cases:
        counts[stratum(c)][c.case.case_id in test_ids] += 1
    RECORD.write_text(
        json.dumps(
            {
                "seed": g.seed,
                "test_size": len(test),
                "dev_size": len(dev),
                "test_ids": sorted(c.case.case_id for c in test),
                "per_stratum_dev_test": {k: counts[k] for k in sorted(counts)},
            },
            indent=2,
        )
        + "\n"
    )
    print(f"dev {len(dev)} · test {len(test)}  (recorded in {RECORD.name})")
    for key in sorted(counts):
        print(f"  {key:22} dev {counts[key][0]:3}  test {counts[key][1]:2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
