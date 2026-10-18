"""Human-facing status and cycle reports.

The hypervisor session is a *view* on the machine state: after ``/new`` a
single ``hv status`` must restore the whole picture, and the cycle report has
a fixed shape so it never has to be re-read twice.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import decisions
from .config import Config
from .state import State

__all__ = ["status_text", "status_json", "cycle_report", "smells", "cost_report"]

NEXT_ACTION = {
    "IDLE": "hv run --until blocked  (opens the next cycle)",
    "PLANNING": "hv run  (architect plans or revises the increment)",
    "ENV": "hv run  (enabling builds or repairs the environment)",
    "DEV": "hv run  (developer implements the increment)",
    "QA": "hv run  (QA verifies against the criteria)",
    "ACCEPT": "hv run  (architect accepts, then squash + merge + regression gate)",
    "AWAITING_HUMAN": "read `awaiting`, write a decision, then resolve",
    "BLOCKED": "inspect the reason, fix the cause, `hv rebuild` re-derives STATE",
    "DONE": "every increment in PLAN.md is accepted",
}


def _short(sha: str | None) -> str:
    return (sha or "-")[:12]


def smells(cfg: Config, state: State) -> list[str]:
    found: list[str] = []
    try:
        history = decisions.load_decisions(cfg.spec_worktree)
    except OSError:
        history = []
    cycle = state.cycle
    criteria_changes = [
        decision for decision in history
        if decision.kind in ("criteria-change", "reopen") and decision.cycle in (cycle, None)
    ]
    if len(criteria_changes) >= 2:
        found.append(
            f"criteria churn: {len(criteria_changes)} changes in cycle {cycle} — "
            "the plan, not the code, is the problem"
        )
    if state.get("reopen_count"):
        found.append(f"increment reopened {state['reopen_count']}x (criteria changed mid-cycle)")
    failed: dict[str, int] = {}
    if cfg.log_path.exists():
        for line in cfg.log_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "qa_failed" and event.get("cycle") == cycle:
                for criterion in event.get("criteria") or []:
                    failed[criterion] = failed.get(criterion, 0) + 1
    for criterion, count in failed.items():
        if count >= 2:
            found.append(f"QA failed {criterion} {count}x in this cycle")
    rejections = state.counters.get("rejections", {})
    if int(rejections.get("dev", 0)) >= 2:
        found.append(
            f"developer rejected {rejections['dev']}x — decomposition or criteria are off"
        )
    return found


def status_text(cfg: Config, state: State) -> str:
    gates = state.get("gates") or {}
    counters = state.get("counters") or {}
    attempts = state.get("attempt") or {}
    lines: list[str] = []
    lines.append(f"{cfg.name} · cycle {state.cycle:02d} · increment {state.get('phase_id') or '-'}")
    lines.append(f"  stage:      {state.stage}")
    lines.append(f"  plan:       v{state.get('plan_ver')} sha {_short(state.get('plan_sha'))} · "
                 f"criteria v{state.get('criteria_version')} · env {_short(state.get('env_sha'))}")
    lines.append(f"  branch:     {state.get('branch') or '-'}  ({state.get('worktree') or '-'})")
    lines.append(f"  attempts:   env {attempts.get('env', 0)} · dev {attempts.get('dev', 0)} · "
                 f"qa {attempts.get('qa', 0)} · architect {attempts.get('architect', 0)}")
    lines.append(f"  rejections: dev {counters.get('rejections', {}).get('dev', 0)} · "
                 f"qa {counters.get('rejections', {}).get('qa', 0)} · "
                 f"total {counters.get('total_rejections', 0)}")
    lines.append(f"  crashes:    {counters.get('crashes', {})} · env repairs "
                 f"{counters.get('env_attempts', 0)}")
    if gates:
        rendered = ", ".join(f"{name} {value}" for name, value in gates.items())
        lines.append(f"  gates:      {rendered}")
    lines.append(f"  isolation:  {state.get('isolation')}")
    cost = state.get("cost") or {}
    sessions = cost.get("sessions") or []
    if sessions:
        lines.append(f"  cost:       ${float(cost.get('total_usd') or 0):.4f} "
                     f"over {len(sessions)} sessions")
    if state.get("awaiting"):
        awaiting = state["awaiting"]
        lines.append("")
        lines.append(f"  WAITING ON HUMAN: {awaiting.get('reason')}")
        for option in awaiting.get("options") or []:
            lines.append(f"    · {option}")
    found = smells(cfg, state)
    if found:
        lines.append("")
        lines.append("  smells:")
        for smell in found:
            lines.append(f"    ! {smell}")
    lines.append("")
    lines.append(f"  next: {NEXT_ACTION.get(state.stage, 'hv status')}")
    return "\n".join(lines)


def status_json(cfg: Config, state: State) -> str:
    payload = dict(state.data)
    payload["smells"] = smells(cfg, state)
    payload["next"] = NEXT_ACTION.get(state.stage)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def cycle_report(cfg: Config, state: State, cycle: int) -> str:
    """The fixed-shape cycle report."""
    plan_sha = _short(state.get("plan_sha"))
    increment = state.get("phase_id") or "-"
    gates = state.get("gates") or {}
    details = state.get("gate_details") or {}
    cost = state.get("cost") or {}
    sessions = cost.get("sessions") or []
    attempts = state.get("attempt") or {}

    def gate_line(name: str) -> str:
        value = gates.get(name)
        return "—" if value is None else str(value)

    lines: list[str] = []
    lines.append(f"cycle {cycle:02d} · increment {increment}")
    lines.append(f"  plan:        sha {plan_sha} · criteria v{state.get('criteria_version')}")
    lines.append(
        "  gates:       build " + gate_line("build")
        + " · tests " + gate_line("test")
        + " · lint " + gate_line("lint")
        + " · regression " + gate_line("regression")
    )
    cold = details.get("setup_cold") or details.get("setup") or {}
    if cold:
        cold_status = cold.get("status") if isinstance(cold, dict) else None
        cold_flag = cold.get("cold") if isinstance(cold, dict) else None
        lines.append(f"  environment: setup {cold_status or '—'} (cold={cold_flag})")
    lines.append(
        f"  qa verdict:  {gates.get('qa') or '—'} · attempts dev {attempts.get('dev', 0)}, "
        f"qa {attempts.get('qa', 0)}"
    )
    lines.append(
        f"  counters:    rejections dev {state.counters['rejections'].get('dev', 0)}, "
        f"qa {state.counters['rejections'].get('qa', 0)}; "
        f"crashes {sum((state.counters.get('crashes') or {}).values())}"
    )
    if sessions:
        lines.append(f"  cost:        ${float(cost.get('total_usd') or 0):.4f}, "
                     f"sessions {len(sessions)}")
    if state.get("awaiting"):
        awaiting = state["awaiting"]
        lines.append(f"  waiting:     {awaiting.get('reason')}")
        for option in awaiting.get("options") or []:
            lines.append(f"               · {option}")
    else:
        lines.append("  waiting:     —")
    lines.append(f"  next:        {NEXT_ACTION.get(state.stage, '-')}")
    found = smells(cfg, state)
    if found:
        lines.append("  smells:      " + "; ".join(found))
    return "\n".join(lines)


def cost_report(cfg: Config, cycle: int) -> Path | None:
    path = Path(cfg.root) / cfg.reports_rel / f"cost-{cycle:02d}.json"
    return path if path.exists() else None
