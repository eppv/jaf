"""DECISIONS.md: append-only decision log on the spec branch.

Nothing in the project may be true only in a chat. A decision exists once it
is written here, which is why `hv resolve`, `hv approve` and `hv reopen`
refuse to run without a decision id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Decision", "load_decisions", "append_decision", "next_id", "DECISIONS_FILE"]

DECISIONS_FILE = "DECISIONS.md"

HEADER = """# DECISIONS

Append-only. One entry per decision, newest last. Written by `hv decide`
(architect or human) — never edited in place.

"""

ENTRY_RE = re.compile(r"^## (D-\d{3,})\s*$", re.MULTILINE)
FIELD_RE = re.compile(r"^-\s+([a-z_]+):\s*(.*)$")


@dataclass
class Decision:
    id: str
    ts: str
    actor: str
    kind: str
    cycle: int | None
    plan_sha: str
    text: str

    def as_markdown(self) -> str:
        lines = [
            f"## {self.id}",
            f"- ts: {self.ts}",
            f"- actor: {self.actor}",
            f"- kind: {self.kind}",
            f"- cycle: {self.cycle if self.cycle is not None else '-'}",
            f"- plan_sha: {self.plan_sha or '-'}",
            f"- text: {self.text}",
            "",
        ]
        return "\n".join(lines)


def _parse_block(block: str) -> Decision | None:
    lines = block.splitlines()
    if not lines:
        return None
    head = ENTRY_RE.match(lines[0].strip())
    if not head:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:]:
        match = FIELD_RE.match(line.strip())
        if match:
            fields[match.group(1)] = match.group(2).strip()
    cycle_raw = fields.get("cycle", "-")
    try:
        cycle = int(cycle_raw) if cycle_raw not in ("-", "") else None
    except ValueError:
        cycle = None
    return Decision(
        id=head.group(1),
        ts=fields.get("ts", ""),
        actor=fields.get("actor", ""),
        kind=fields.get("kind", "note"),
        cycle=cycle,
        plan_sha=fields.get("plan_sha", ""),
        text=fields.get("text", ""),
    )


def load_decisions(spec_worktree: Path) -> list[Decision]:
    path = Path(spec_worktree) / DECISIONS_FILE
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## D-\d{3,}\s*$)", text)
    decisions: list[Decision] = []
    for part in parts:
        if not part.lstrip().startswith("## D-"):
            continue
        decision = _parse_block(part)
        if decision:
            decisions.append(decision)
    return decisions


def next_id(decisions: list[Decision]) -> str:
    highest = 0
    for decision in decisions:
        try:
            highest = max(highest, int(decision.id.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return f"D-{highest + 1:03d}"


def append_decision(spec_worktree: Path, *, kind: str, text: str, actor: str,
                    cycle: int | None = None, plan_sha: str = "") -> Decision:
    path = Path(spec_worktree) / DECISIONS_FILE
    if not path.exists():
        path.write_text(HEADER, encoding="utf-8")
    decisions = load_decisions(spec_worktree)
    decision = Decision(
        id=next_id(decisions),
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        actor=actor,
        kind=kind,
        cycle=cycle,
        plan_sha=plan_sha,
        text=text.replace("\n", " ").strip(),
    )
    with path.open("a", encoding="utf-8") as handle:
        if path.stat().st_size and not path.read_text(encoding="utf-8").endswith("\n"):
            handle.write("\n")
        handle.write(decision.as_markdown())
    return decision


def find_decision(spec_worktree: Path, decision_id: str) -> Decision | None:
    for decision in load_decisions(spec_worktree):
        if decision.id == decision_id:
            return decision
    return None
