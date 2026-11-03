"""Writes the synthetic case set: sampled briefs in, realistic message text out.

    python -m data.generate --max-usd 3

Each brief comes from data/spec.py with its label already decided; Claude only writes
the words. Finished cases append to data/cases_all.jsonl as they land, so a re-run
picks up where the last one stopped. Afterward, `python -m data.split` makes the
dev/test split (free, no API calls).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from rich.console import Console

from triage.agents import cost_usd, render_case
from triage.config import Settings, load_settings
from triage.run import preflight
from triage.schemas import CallUsage, Case, LabeledCase

from .spec import DATA_DIR, CaseBrief, describe, sample_briefs

ROOT = DATA_DIR.parent
OUT = DATA_DIR / "cases_all.jsonl"
MAX_ATTEMPTS = 2

# Label vocabulary that shouldn't appear in a sender's own words. Injection cases are
# exempt: putting labels in front of the triage system is their whole point.
LABEL_LEAK = re.compile(
    r"\bP[1-4]\b|\bTier ?[12]\b|\btriage\b|\bHuman-Review\b|\b(?:category|queue|priority)\s*:", re.IGNORECASE
)


class WrittenCase(BaseModel):
    subject: str | None
    body: str


class WriterError(RuntimeError):
    def __init__(self, message: str, usage: CallUsage | None = None):
        super().__init__(message)
        self.usage = usage


def load_writer_prompt() -> str:
    text = (DATA_DIR / "writer.md").read_text()
    return re.sub(r"<!--.*?-->\s*", "", text, flags=re.DOTALL).strip()


def render_brief(b: CaseBrief) -> str:
    who = {
        "Resident": f"{b.sender_name}, a resident",
        "Property Manager": f"{b.sender_name}, the community manager",
        "Unknown": b.sender_name,
    }[b.contact_role.value]
    if b.contact_role.value != "Resident":
        place = b.property_name
    elif b.mentions_unit:
        place = f"{b.property_name}, unit {b.unit}"
    else:
        place = f"{b.property_name} (they don't mention their unit number)"
    lines = [
        f"Sender: {who}",
        f"Property: {place}",
        f"Sent: {b.received_at:%A at %-I:%M %p}, by {b.channel.value}",
        f"Language: {b.language}",
        f"Tone: {b.tone}",
        f"Writing: {b.polish}",
        f"Length: {b.length}",
        f"Situation: {b.scenario.premise}",
    ]
    if b.secondary:
        lines.append(f"Also mention a second, smaller issue: {b.secondary}")
    if b.special:
        lines.append(f"Special instruction: {b.special}")
    return "<brief>\n" + "\n".join(lines) + "\n</brief>"


def leaked_label(b: CaseBrief, written: WrittenCase) -> str | None:
    if b.hard_type == "injection":
        return None
    m = LABEL_LEAK.search(f"{written.subject or ''}\n{written.body}")
    return m.group(0) if m else None


class CaseWriter:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self._client = client or anthropic.Anthropic()
        self._gen = settings.generator
        self._price = settings.prices[settings.generator.model]
        self._system = load_writer_prompt()

    def _call(self, brief: CaseBrief, nudge: str | None) -> tuple[WrittenCase, CallUsage]:
        user = render_brief(brief) + (f"\n\n{nudge}" if nudge else "")
        start = time.perf_counter()
        try:
            response = self._client.messages.parse(
                model=self._gen.model,
                max_tokens=self._gen.max_tokens,
                system=[{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self._gen.effort},
                output_format=WrittenCase,
            )
        except ValidationError as err:
            raise WriterError(f"{brief.case_id}: output failed schema validation: {err}") from err
        usage = CallUsage(
            stage="write",
            model=self._gen.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
            cost_usd=cost_usd(self._price, response.usage),
            latency_s=time.perf_counter() - start,
        )
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise WriterError(f"{brief.case_id}: stop_reason={response.stop_reason}", usage=usage)
        return response.parsed_output, usage

    def write(self, brief: CaseBrief) -> tuple[WrittenCase, list[CallUsage], list[str]]:
        """Writes one case, retrying once if label words leak into the sender's text."""
        usages, warnings, nudge = [], [], None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            written, usage = self._call(brief, nudge)
            usages.append(usage)
            leak = leaked_label(brief, written)
            if leak is None:
                break
            warnings.append(f"attempt {attempt} used label word {leak!r}")
            nudge = f"Your last draft used the word {leak!r}, which a real sender wouldn't write. Rewrite without it."
        return written, usages, warnings


CASE_FIELDS = ("case_id", "channel", "received_at", "contact_role", "property_id")


def fingerprint(brief: CaseBrief) -> str:
    return json.dumps(brief.model_dump(mode="json"), sort_keys=True)


def stored_fingerprint(row: dict) -> str:
    """Rebuilds the brief a stored case was written from, in the same form as fingerprint()."""
    brief = {k: v for k, v in (row.get("brief") or {}).items() if k != "generation"}
    brief |= {k: row["case"][k] for k in CASE_FIELDS}
    return json.dumps(CaseBrief.model_validate(brief).model_dump(mode="json"), sort_keys=True)


def stale_ids(briefs: list[CaseBrief], path: Path) -> list[str]:
    """Cases already in the file that were written from a different brief than today's config
    produces, e.g. after scenarios.yaml or the sampler changed. Resuming over them would mix two datasets."""
    if not path.exists():
        return []
    current = {b.case_id: fingerprint(b) for b in briefs}
    stale = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row["case"]["case_id"]
        try:
            same = case_id not in current or stored_fingerprint(row) == current[case_id]
        except ValidationError:
            same = False
        if not same:
            stale.append(case_id)
    return stale


def to_labeled(brief: CaseBrief, written: WrittenCase, usages: list[CallUsage], warnings: list[str]) -> LabeledCase:
    case = Case(
        case_id=brief.case_id,
        channel=brief.channel,
        received_at=brief.received_at,
        contact_role=brief.contact_role,
        property_id=brief.property_id,
        subject=written.subject,
        body=written.body,
    )
    record = brief.model_dump(mode="json", exclude=set(CASE_FIELDS))
    record["generation"] = {
        "model": usages[-1].model,
        "attempts": len(usages),
        "cost_usd": round(sum(u.cost_usd for u in usages), 6),
        "warnings": warnings,
    }
    return LabeledCase(
        case=case,
        label=brief.label,
        hard_case_tags=[brief.hard_type] if brief.hard_type else [],
        label_source="spec",
        brief=record,
    )


def done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {json.loads(line)["case"]["case_id"] for line in path.read_text().splitlines() if line.strip()}


@dataclass
class GenerationOutcome:
    written: int = 0
    spent: float = 0.0
    failed: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)
    stopped_by: str | None = None


def generate(
    briefs: list[CaseBrief],
    writer: CaseWriter,
    out: Path,
    max_usd: float,
    workers: int = 4,
    on_case=None,
) -> GenerationOutcome:
    """Writes every brief not already in `out`. Stops submitting once spend reaches max_usd;
    cases already in flight finish, so a run can pass the cap by at most `workers` cases."""
    outcome = GenerationOutcome()
    if stale := stale_ids(briefs, out):
        outcome.stopped_by = (
            f"{len(stale)} cases in {out.name} were written from different briefs ({', '.join(stale[:4])}...). "
            "Move that file aside and run again"
        )
        return outcome
    already = done_ids(out)
    todo = iter([b for b in briefs if b.case_id not in already])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        running: dict[Future, CaseBrief] = {}

        def submit_next() -> None:
            if outcome.stopped_by is None and outcome.spent < max_usd and (brief := next(todo, None)):
                running[pool.submit(writer.write, brief)] = brief

        for _ in range(workers):
            submit_next()
        while running:
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                brief = running.pop(future)
                try:
                    written, usages, warnings = future.result()
                except WriterError as err:
                    outcome.failed.append(str(err))
                    outcome.spent += err.usage.cost_usd if err.usage else 0.0
                except anthropic.APIError as err:  # bad key, rate limit after retries, network
                    outcome.stopped_by = f"{type(err).__name__}: {err}"
                else:
                    labeled = to_labeled(brief, written, usages, warnings)
                    with out.open("a") as f:
                        f.write(labeled.model_dump_json() + "\n")
                    outcome.written += 1
                    outcome.spent += sum(u.cost_usd for u in usages)
                    if warnings:
                        outcome.warned.append(f"{brief.case_id}: {'; '.join(warnings)}")
                    if on_case:
                        on_case(labeled, sum(u.cost_usd for u in usages))
                submit_next()
    if outcome.stopped_by is None and outcome.spent >= max_usd and done_ids(out) < {b.case_id for b in briefs}:
        outcome.stopped_by = f"spend cap of ${max_usd:.2f} reached"
    return outcome


def sort_file(path: Path) -> None:
    """Rewrites the file in case-id order; cases land in completion order."""
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    lines.sort(key=lambda line: json.loads(line)["case"]["case_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-usd", type=float, required=True, help="spend cap for this run")
    parser.add_argument("--limit", type=int, help="write only the first N briefs (for a trial run)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--show", type=int, default=0, help="print the text of the first N cases written")
    args = parser.parse_args(argv)
    console = Console()

    load_dotenv(ROOT / ".env")
    if reason := preflight(os.environ):
        console.print(f"[red]{reason}[/]")
        return 2

    settings = load_settings()
    g = settings.generator
    briefs = sample_briefs(g.cases, g.hard_share, g.seed, g.test_size)[: args.limit]
    remaining = len(briefs) - len(done_ids(args.out) & {b.case_id for b in briefs})
    console.print(
        f"[bold]Writing {remaining} of {len(briefs)} cases[/] with {g.model} ({g.effort}) · "
        f"cap ${args.max_usd:.2f} · {args.workers} at a time"
    )
    shown = 0

    def progress(labeled: LabeledCase, cost: float) -> None:
        nonlocal shown
        tag = f" [{labeled.hard_case_tags[0]}]" if labeled.hard_case_tags else ""
        console.print(
            f"  {labeled.case.case_id}  {labeled.label.category.value} · {labeled.label.priority.value}{tag}  "
            f"[dim]{len(labeled.case.body)} chars  ${cost:.4f}[/]"
        )
        if shown < args.show:
            shown += 1
            console.print(f"[dim]{render_case(labeled.case)}[/]\n")

    outcome = generate(briefs, CaseWriter(settings), args.out, args.max_usd, args.workers, progress)
    if args.out.exists():
        sort_file(args.out)

    total = len(done_ids(args.out))
    console.print(f"\nWrote {outcome.written} this run, ${outcome.spent:.4f}. {total} cases now in {args.out.name}.")
    console.print(json.dumps(describe(briefs), indent=None))
    for line in outcome.warned:
        console.print(f"[yellow]Retried:[/] {line}")
    for line in outcome.failed:
        console.print(f"[red]Failed:[/] {line}")
    if outcome.stopped_by:
        console.print(f"[bold red]Stopped early:[/] {outcome.stopped_by}. Run the same command again to finish.")
        return 1
    console.print("Next: python -m data.split")
    return 0


if __name__ == "__main__":
    sys.exit(main())
