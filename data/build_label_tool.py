"""Builds data/label_tool.html: one static page with every gold case embedded, for labeling by hand.

    python -m data.build_label_tool

Definitions come straight from triage/prompts/taxonomy.md, so the page and the
prompts can't drift apart. Labels save in the browser (localStorage); "Download
labels" or "Copy for Claude" hands them to `python -m data.merge_labels`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from triage.schemas import Category, EscalationFlag, LabeledCase, Priority, Queue

from .spec import DATA_DIR, PROPERTIES, QUEUE_BY_CATEGORY
from .split import TEST, read_cases

ROOT = DATA_DIR.parent
TEMPLATE = DATA_DIR / "label_tool.template.html"
OUT = DATA_DIR / "label_tool.html"
TAXONOMY = ROOT / "triage" / "prompts" / "taxonomy.md"


def section_items(markdown: str, heading: str) -> dict[str, str]:
    """The `- **Term**: definition` bullets under a `## heading` in taxonomy.md."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", markdown, re.MULTILINE | re.DOTALL)
    items = re.findall(r"^- \*\*(.+?)\*\*: (.+)$", m.group(1), re.MULTILINE) if m else []
    flag_items = re.findall(r"^- `(.+?)`: (.+)$", m.group(1), re.MULTILINE) if m else []
    return dict(items + flag_items)


def page_data(cases: list[LabeledCase]) -> dict:
    md = TAXONOMY.read_text()
    return {
        "taxonomy": {
            "categories": [c.value for c in Category],
            "priorities": [p.value for p in Priority],
            "flags": [f.value for f in EscalationFlag],
            "queues": [q.value for q in Queue],
            "queue_by_category": {c.value: q.value for c, q in QUEUE_BY_CATEGORY.items()},
            "tier2_flags": [EscalationFlag.SAFETY.value, EscalationFlag.PROPERTY_WIDE_OUTAGE.value],
            "tier2_queue": Queue.TIER2_ESCALATIONS.value,
            "category_help": section_items(md, "Categories"),
            "priority_help": section_items(md, "Priorities"),
            "flag_help": section_items(md, "Escalation flags"),
        },
        "cases": [
            {
                "id": c.case.case_id,
                "channel": c.case.channel.value,
                "received": f"{c.case.received_at:%a %b %-d · %-I:%M %p}",
                "role": c.case.contact_role.value,
                "property": PROPERTIES.get(c.case.property_id or "", c.case.property_id or "no property"),
                "subject": c.case.subject,
                "body": c.case.body,
                "spec": (c.review.spec_label if c.review else c.label).model_dump(mode="json"),
                "hard": c.hard_case_tags[0] if c.hard_case_tags else None,
                "premise": (c.brief or {}).get("scenario", {}).get("premise", ""),
                "alt": (c.brief or {}).get("scenario", {}).get("alt"),
                "language": (c.brief or {}).get("language", "English"),
            }
            for c in sorted(cases, key=lambda c: c.case.case_id)
        ],
    }


def build(cases: list[LabeledCase], head_extra: str = "", footer: str = "") -> str:
    payload = json.dumps(page_data(cases), ensure_ascii=False).replace("</", "<\\/")
    return (
        TEMPLATE.read_text()
        .replace("__TB_DATA__", payload)
        .replace("<!--HEAD_EXTRA-->", head_extra)
        .replace("<!--FOOTER-->", footer)
    )


def main() -> int:
    if not TEST.exists():
        print(f"No {TEST.name} yet. Run python -m data.generate, then python -m data.split.")
        return 2
    cases = read_cases(TEST)
    OUT.write_text(build(cases))
    print(f"Wrote {OUT.relative_to(ROOT)} with {len(cases)} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
