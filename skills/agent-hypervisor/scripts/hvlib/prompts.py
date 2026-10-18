"""Prompt composition.

A session prompt is the role contract plus exactly the context that stage
needs: the increment pinned by ``plan_sha``, the previous artifact and any
blocking defects. The code itself is never restated — the agent reads the
worktree — otherwise the context-window argument for splitting the work
disappears.

All wording lives in ``templates/``; this module only assembles it.
"""

from __future__ import annotations

from . import artifacts
from .config import attempt_key
from .state import State

__all__ = [
    "build",
    "assignment",
    "prompt_template",
    "previous_artifacts",
    "pending_defects",
]

# The role template is rendered first and the assignment substituted afterwards,
# so that braces inside artifact excerpts never reach the renderer twice.
ASSIGNMENT_SENTINEL = "\x00assignment\x00"


def prompt_template(dispatcher, name: str) -> str:
    path = dispatcher.skill_root / "templates" / "prompts" / name
    if not path.exists():
        raise FileNotFoundError(f"prompt fragment missing: {path}")
    return path.read_text(encoding="utf-8")


def build(dispatcher, role: str, *, increment=None, plan=None,
          mode: str | None = None, attempt: int | None = None) -> str:
    template_path = dispatcher.skill_root / "templates" / "roles" / f"{role}.md"
    if not template_path.exists():
        raise FileNotFoundError(f"role template missing: {template_path}")
    template = template_path.read_text(encoding="utf-8")
    if increment is None and dispatcher.state.get("phase_id"):
        increment = dispatcher.increment(plan)
    text = assignment(dispatcher, role, increment, mode=mode, attempt=attempt)
    values = {
        "project": dispatcher.cfg.name,
        "cycle": f"{dispatcher.state.cycle:02d}",
        "role": role,
        "workdir": str(dispatcher.worktree),
        "spec_dir": str(dispatcher.worktree / dispatcher.cfg.path("spec").name),
        "reports_dir": dispatcher.cfg.reports_rel,
        "plan_sha": str(dispatcher.state.get("plan_sha") or ""),
        "criteria_version": str(dispatcher.state.get("criteria_version") or ""),
        "increment_id": increment.id if increment else "-",
        "assignment": ASSIGNMENT_SENTINEL,
    }
    return artifacts.render_prompt(template, values).replace(ASSIGNMENT_SENTINEL, text)


def assignment(dispatcher, role: str, increment, *, mode: str | None = None,
               attempt: int | None = None) -> str:
    key = attempt_key(role)
    state: State = dispatcher.state
    attempt = int(attempt or (int(state["attempt"].get(key, 0)) + 1))
    base = {
        "project": dispatcher.cfg.name,
        "cycle": f"{state.cycle:02d}",
        "attempt": str(attempt),
        "rules": prompt_template(dispatcher, "rules.md"),
        "workdir": str(dispatcher.worktree),
        "spec_dir": str(dispatcher.worktree / dispatcher.cfg.path("spec").name),
        "reports_dir": dispatcher.cfg.reports_rel,
    }
    if increment is None:
        return artifacts.render_prompt(
            prompt_template(dispatcher, "assignment_planning.md"), base
        )

    criteria: list[str] = []
    for criterion in increment.acceptance:
        criteria.append(f"- `{criterion.id}` (v{criterion.version}): {criterion.text}")
        if criterion.check:
            criteria.append(f"  check: `{criterion.check}`")

    pins = ""
    if role != "architect" or mode == "accept":
        pins = (f"- plan_sha: `{state.get('plan_sha')}`\n"
                f"- criteria_version: `{state.get('criteria_version')}`\n")

    depends_on = ""
    if increment.depends_on:
        depends_on = f"depends_on: {', '.join(increment.depends_on)}\n"

    previous = previous_artifacts(dispatcher, role)
    defects = pending_defects(dispatcher, role)

    return artifacts.render_prompt(
        prompt_template(dispatcher, "assignment.md"),
        {
            **base,
            "increment_id": increment.id,
            "increment_title": increment.title,
            "pins": pins,
            "depends_on": depends_on,
            "criteria": "\n".join(criteria),
            "body": increment.body.strip(),
            "previous": f"## Previous artifacts\n\n{previous}" if previous else "",
            "defects": ("## Blocking defects from the previous attempt\n\n"
                        f"{defects}") if defects else "",
        },
    )


def previous_artifacts(dispatcher, role: str) -> str:
    """The one artifact a role is allowed to depend on, quoted verbatim."""
    state: State = dispatcher.state
    attempts = state.get("attempt") or {}
    if role == "dev":
        qa_attempt = int(attempts.get("qa", 0))
        if qa_attempt:
            path = dispatcher.report(artifacts.QA_REPORT, qa_attempt)
            if path.exists():
                return (f"QA verdict from attempt {qa_attempt} (`{path.name}`):\n\n"
                        f"{path.read_text(encoding='utf-8')}")
    if role == "qa":
        dev_attempt = int(attempts.get("dev", 0))
        path = dispatcher.report(artifacts.STAGE_REPORT, dev_attempt)
        if path.exists():
            return (f"Developer report (`{path.name}`), treat it as a hypothesis:\n\n"
                    f"{path.read_text(encoding='utf-8')}")
    if role == "architect":
        qa_attempt = int(attempts.get("qa", 0))
        if qa_attempt:
            path = dispatcher.report(artifacts.QA_REPORT, qa_attempt)
            if path.exists():
                return f"QA verdict (`{path.name}`):\n\n{path.read_text(encoding='utf-8')}"
        stage_attempt = int(attempts.get("dev", 0))
        path = dispatcher.report(artifacts.STAGE_REPORT, stage_attempt)
        if path.exists():
            return f"Developer report (`{path.name}`):\n\n{path.read_text(encoding='utf-8')}"
    return ""


def pending_defects(dispatcher, role: str) -> str:
    """Rejection or retry note left for this role's next attempt."""
    if role not in ("dev", "enabling"):
        return ""
    state: State = dispatcher.state
    attempt = int(state["attempt"].get(attempt_key(role), 1))
    reports = dispatcher.report(artifacts.STAGE_REPORT, attempt).parent
    candidates = [
        reports / f"RETRY-{role}-{state.cycle:02d}-attempt-{attempt}.md",
        reports / f"REJECT-{state.cycle:02d}-attempt-{attempt}.md",
        reports / f"REJECT-{state.cycle:02d}-attempt-{attempt - 1}.md",
        reports / f"RETRY-{role}-{state.cycle:02d}-attempt-{attempt - 1}.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""
