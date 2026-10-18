"""PLAN.md parsing, increment selection and plan hashing.

The plan is the contract. Everything the dispatcher knows about "what to
build next" and "what counts as done" comes from this file, and its hash is
what binds an artifact to the contract it was produced against.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import miniyaml

__all__ = [
    "Criterion",
    "Increment",
    "Plan",
    "PlanError",
    "load_plan",
    "normalize_block",
    "parse_plan_text",
]

PLAN_FILE = "PLAN.md"
SECTION_RE = re.compile(r"^##\s+.*?(\d+(?:\.\d+)*)\s*(.*)$")


class PlanError(ValueError):
    """PLAN.md is missing the structure the protocol requires."""


def normalize_block(text: str) -> str:
    """Normalise a text block before hashing.

    Trailing whitespace is dropped, runs of blank lines collapse to one and
    leading/trailing blank lines disappear, so cosmetic edits do not invalidate
    artifacts. The ``plan_sha`` field is stripped by the caller.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(line)
        blank = False
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


@dataclass
class Criterion:
    id: str
    version: int
    text: str
    check: str

    def snapshot(self) -> dict:
        return {
            "version": self.version,
            "text_sha": hashlib.sha256(normalize_block(self.text).encode()).hexdigest()[:16],
            "check_sha": hashlib.sha256(normalize_block(self.check).encode()).hexdigest()[:16],
        }


@dataclass
class Increment:
    id: str
    title: str
    depends_on: list[str]
    criteria_version: int
    acceptance: list[Criterion]
    body: str
    accepted_in: int | None = None
    raw: dict = field(default_factory=dict)

    def entry_for_hash(self) -> dict:
        entry = {
            key: value
            for key, value in self.raw.items()
            if key not in ("plan_sha", "accepted_in")
        }
        entry["acceptance"] = [
            {"id": c.id, "version": c.version, "text": c.text, "check": c.check}
            for c in self.acceptance
        ]
        return entry

    def plan_sha(self) -> str:
        payload = {
            "entry": self.entry_for_hash(),
            "body": normalize_block(self.body),
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def short_sha(self) -> str:
        return self.plan_sha()[:12]

    def criteria_snapshot(self) -> dict:
        return {c.id: c.snapshot() for c in self.acceptance}

    def criterion(self, cid: str) -> Criterion | None:
        for criterion in self.acceptance:
            if criterion.id == cid:
                return criterion
        return None


@dataclass
class Plan:
    path: Path
    plan_ver: int
    increments: list[Increment]
    text: str

    def get(self, increment_id) -> Increment | None:
        wanted = str(increment_id)
        for increment in self.increments:
            if increment.id == wanted:
                return increment
        return None

    def accepted_ids(self) -> list[str]:
        return [inc.id for inc in self.increments if inc.accepted_in]

    def next_increment(self, include_accepted: bool = False) -> Increment | None:
        accepted = set(self.accepted_ids())
        for increment in self.increments:
            if increment.accepted_in and not include_accepted:
                continue
            deps = [str(dep) for dep in increment.depends_on]
            if all(dep in accepted for dep in deps):
                return increment
        return None


def _sections(text: str) -> dict[str, str]:
    """Map increment id -> body text (heading excluded) from the markdown body."""
    sections: dict[str, str] = {}
    current_id: str | None = None
    buffer: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current_id is not None:
                sections[current_id] = "\n".join(buffer)
            match = SECTION_RE.match(line)
            current_id = match.group(1) if match else None
            buffer = []
            continue
        if current_id is not None:
            buffer.append(line)
    if current_id is not None:
        sections[current_id] = "\n".join(buffer)
    return sections


def parse_plan_text(text: str, path: Path | None = None) -> Plan:
    front, body = miniyaml.parse_front_matter(text)
    if not front:
        raise PlanError("PLAN.md has no front-matter with `increments:`")
    raw_increments = front.get("increments")
    if not isinstance(raw_increments, list) or not raw_increments:
        raise PlanError("PLAN.md front-matter must contain a non-empty `increments:` list")
    sections = _sections(body)

    increments: list[Increment] = []
    seen_ids: set[str] = set()
    for raw in raw_increments:
        if not isinstance(raw, dict):
            raise PlanError(f"increment entry must be a mapping, got {raw!r}")
        inc_id = str(raw.get("id") or "").strip()
        if not inc_id:
            raise PlanError("increment entry without `id`")
        if inc_id in seen_ids:
            raise PlanError(f"duplicate increment id {inc_id!r}")
        seen_ids.add(inc_id)
        raw_acceptance = raw.get("acceptance") or []
        if not isinstance(raw_acceptance, list):
            raise PlanError(f"increment {inc_id}: acceptance must be a list")
        criteria: list[Criterion] = []
        seen_criteria: set[str] = set()
        for item in raw_acceptance:
            if not isinstance(item, dict):
                raise PlanError(f"increment {inc_id}: criterion must be a mapping")
            cid = str(item.get("id") or "").strip()
            if not cid:
                raise PlanError(f"increment {inc_id}: criterion without id")
            if cid in seen_criteria:
                raise PlanError(f"increment {inc_id}: duplicate criterion id {cid!r}")
            seen_criteria.add(cid)
            check = str(item.get("check") or "").strip()
            criteria.append(
                Criterion(
                    id=cid,
                    version=int(item.get("version") or 1),
                    text=str(item.get("text") or "").strip(),
                    check=check,
                )
            )
        depends_raw = raw.get("depends_on") or []
        if not isinstance(depends_raw, list):
            raise PlanError(f"increment {inc_id}: depends_on must be a list")
        accepted_in = raw.get("accepted_in")
        increments.append(
            Increment(
                id=inc_id,
                title=str(raw.get("title") or "").strip(),
                depends_on=[str(dep) for dep in depends_raw],
                criteria_version=int(raw.get("criteria_version") or 1),
                acceptance=criteria,
                body=sections.get(inc_id, "").strip("\n"),
                accepted_in=int(accepted_in) if accepted_in not in (None, "", 0) else None,
                raw=raw,
            )
        )

    return Plan(
        path=path or Path(PLAN_FILE),
        plan_ver=int(front.get("plan_ver") or 1),
        increments=increments,
        text=text,
    )


def load_plan(path: Path) -> Plan:
    path = Path(path)
    if not path.exists():
        raise PlanError(f"{path} not found")
    return parse_plan_text(path.read_text(encoding="utf-8"), path)


def validate_plan(plan: Plan) -> list[str]:
    """Return a list of protocol violations (empty means the plan is usable)."""
    problems: list[str] = []
    accepted = set(plan.accepted_ids())
    for increment in plan.increments:
        if not increment.title:
            problems.append(f"increment {increment.id}: empty title")
        if not increment.acceptance:
            problems.append(f"increment {increment.id}: no acceptance criteria")
        for dep in increment.depends_on:
            if plan.get(dep) is None:
                problems.append(f"increment {increment.id}: depends_on unknown {dep!r}")
        for criterion in increment.acceptance:
            if not criterion.text:
                problems.append(f"{criterion.id}: empty text")
            if not criterion.check:
                problems.append(f"{criterion.id}: no executable `check`")
            if criterion.version > 1:
                history = increment.raw.get("acceptance_history") or []
                versions = {
                    int(item.get("version") or 0)
                    for item in history
                    if isinstance(item, dict) and str(item.get("id")) == criterion.id
                }
                if not any(version and version < criterion.version for version in versions):
                    problems.append(
                        f"{criterion.id}: version {criterion.version} without a "
                        "previous version in `acceptance_history` (old versions must be kept)"
                    )
    # a dependency cycle is a plan defect, not a runtime surprise
    for increment in plan.increments:
        seen: set[str] = set()
        stack = [increment.id]
        while stack:
            current = stack.pop()
            if current in seen:
                problems.append(f"dependency cycle involving increment {increment.id}")
                break
            seen.add(current)
            node = plan.get(current)
            if node:
                stack.extend(node.depends_on)
    del accepted
    return problems
