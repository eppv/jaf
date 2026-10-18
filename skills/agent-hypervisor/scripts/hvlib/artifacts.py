"""Artifact rendering, schemas and validation.

Reports are the only channel between role sessions, so they are validated
mechanically: a missing section is a defect in the handover, not a style
nit. The dispatcher never "reads nicely" — it checks required keys.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import miniyaml
from .plan import Increment

__all__ = [
    "ArtifactError",
    "SCHEMAS",
    "report_path",
    "render_prompt",
    "validate_report",
    "parse_report",
    "criteria_verdicts",
    "REPORT_DIR",
]

REPORT_DIR = "docs/reports"

STAGE_REPORT = "stage-report"
QA_REPORT = "qa-verdict"
ARCHITECT_REPORT = "architect-report"

SCHEMAS: dict[str, dict] = {
    STAGE_REPORT: {
        "required_sections": [
            "made",
            "deviations",
            "open questions",
            "how to verify",
        ],
        "required_front": ["plan_sha", "criteria_version", "role", "cycle", "attempt"],
    },
    QA_REPORT: {
        "required_sections": ["verdicts", "defects", "debts"],
        "required_front": ["plan_sha", "criteria_version", "role", "cycle", "attempt"],
    },
    ARCHITECT_REPORT: {
        "required_sections": ["decisions", "plan changes", "next"],
        "required_front": ["plan_sha", "criteria_version", "role", "cycle", "attempt"],
    },
}

# Section headings are matched case-insensitively against these aliases, so a
# role session can phrase a heading naturally without breaking validation.
SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "made": ("made", "what was done", "done", "changes"),
    "deviations": ("deviations", "deviations and implicit decisions", "implicit decisions"),
    "open questions": ("open questions",),
    "how to verify": ("how to verify", "verification"),
    "verdicts": ("verdicts", "verdict"),
    "defects": ("defects",),
    "debts": ("debts", "technical debt"),
    "decisions": ("decisions",),
    "plan changes": ("plan changes",),
    "next": ("next", "next increment"),
}

VERDICT_ROW_RE = re.compile(
    r"^\s*[|\-\s]*\s*(?P<id>[A-Za-z]+-\d+(?:\.\d+)*)\s*[|:\s]\s*(?P<verdict>pass|fail|PASS|FAIL)\b",
    re.MULTILINE,
)


class ArtifactError(ValueError):
    """An artifact violates the protocol schema."""


@dataclass
class Report:
    path: Path
    front: dict
    body: str
    schema: str

    def section(self, name: str) -> str:
        return _extract_section(self.body, name)

    @property
    def plan_sha(self) -> str:
        return str(self.front.get("plan_sha") or "")

    @property
    def criteria_version(self) -> int:
        try:
            return int(self.front.get("criteria_version") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def role(self) -> str:
        return str(self.front.get("role") or "")


def report_path(root: Path, reports_rel: str, kind: str, cycle: int, attempt: int,
                role: str | None = None) -> Path:
    prefix = {
        STAGE_REPORT: "STAGE",
        QA_REPORT: "QA",
        ARCHITECT_REPORT: "ARCH",
    }[kind]
    name = f"{prefix}-{cycle:02d}-attempt-{attempt}.md"
    if role:
        name = f"{prefix}-{cycle:02d}-attempt-{attempt}-{role}.md"
    return Path(root) / reports_rel / name


def _norm_heading(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.strip().lower()).strip()


def _extract_section(body: str, name: str) -> str:
    aliases = {_norm_heading(alias) for alias in SECTION_ALIASES.get(name, (name,))}
    lines = body.split("\n")
    collected: list[str] = []
    active = False
    active_level = 0
    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            level = len(match.group(1))
            heading = _norm_heading(match.group(2))
            if heading in aliases and (not active or level <= active_level):
                active = True
                active_level = level
                continue
            if active and level <= active_level:
                active = False
                continue
        if active:
            collected.append(line)
    return "\n".join(collected).strip()


def parse_report(path: Path, schema: str) -> Report:
    path = Path(path)
    if not path.exists():
        raise ArtifactError(f"artifact `{path.name}` was not produced")
    text = path.read_text(encoding="utf-8")
    try:
        front, body = miniyaml.parse_front_matter(text)
    except miniyaml.MiniYAMLError as exc:
        raise ArtifactError(f"{path.name}: malformed front-matter ({exc})") from exc
    if not front:
        raise ArtifactError(f"{path.name}: no front-matter block")
    return Report(path=path, front=front, body=body, schema=schema)


def validate_report(report: Report, increment: Increment, plan_sha: str,
                    criteria_version: int, *, strict_pins: bool = True) -> list[str]:
    """Return protocol violations for a role artifact."""
    schema = SCHEMAS[report.schema]
    problems: list[str] = []
    for key in schema["required_front"]:
        if report.front.get(key) in (None, "", []):
            problems.append(f"front-matter: missing `{key}`")
    for section in schema["required_sections"]:
        if not report.section(section):
            problems.append(
                f"missing required section `{section}` "
                f"(accepted aliases: {', '.join(SECTION_ALIASES.get(section, (section,)))})"
            )
    if strict_pins:
        artifact_sha = str(report.front.get("plan_sha") or "").strip()
        if artifact_sha and artifact_sha != plan_sha:
            problems.append(
                f"plan_sha mismatch: artifact {artifact_sha[:12]} vs plan {plan_sha[:12]}"
            )
        try:
            artifact_cv = int(report.front.get("criteria_version") or 0)
        except (TypeError, ValueError):
            artifact_cv = -1
        if artifact_cv and artifact_cv != criteria_version:
            problems.append(
                f"criteria_version mismatch: artifact {artifact_cv} vs plan {criteria_version}"
            )
    if report.schema == QA_REPORT:
        verdicts = criteria_verdicts(report.body)
        known = {criterion.id for criterion in increment.acceptance}
        for cid in known:
            if cid not in verdicts:
                problems.append(f"verdict missing for criterion {cid}")
        for cid in verdicts:
            if cid not in known:
                problems.append(f"verdict for unknown criterion {cid}")
    return problems


def criteria_verdicts(body: str) -> dict[str, str]:
    """Extract ``criterion id -> pass|fail`` from a QA body."""
    section = _extract_section(body, "verdicts")
    verdicts: dict[str, str] = {}
    for match in VERDICT_ROW_RE.finditer(section):
        verdicts[match.group("id")] = match.group("verdict").lower()
    return verdicts


def body_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def role_template_dir(skill_root: Path) -> Path:
    return Path(skill_root) / "templates" / "roles"


def render_prompt(template: str, values: dict) -> str:
    """Render a role template.

    Placeholders use ``{{name}}`` so that shell braces in commands survive.
    """
    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        if key not in values:
            raise ArtifactError(f"unknown prompt placeholder {{{{{key}}}}}")
        return str(values[key])

    return re.sub(r"\{\{([a-z_]+)\}\}", replace, template)
