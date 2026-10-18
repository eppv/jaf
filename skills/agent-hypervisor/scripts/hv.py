"""hv — dispatcher CLI for the agent-hypervisor protocol.

One process, one writer. Roles write files; this CLI decides what happens
next. Nothing here is a judgement call: every command either applies a rule or
refuses.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hvlib import decisions, gitops, plan as plan_mod, report as report_mod  # noqa: E402
from hvlib.config import CONFIG_NAME, Config, ConfigError, load_config  # noqa: E402
from hvlib.dispatch import Dispatcher, DispatcherError  # noqa: E402
from hvlib.state import (  # noqa: E402
    State,
    StateError,
    load_state,
    log_event,
    new_state,
    now,
    save_state,
)

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = SKILL_ROOT / "templates"

ROLE_ENV = "HV_ROLE"
READ_ONLY_FOR_ROLE = {"status", "report", "log", "plan"}


def echo(message: str) -> None:
    print(message, flush=True)


def find_root(start: Path) -> Path:
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).exists():
            return candidate
    raise SystemExit(
        f"hv.toml not found in {current} or any parent — run `hv init` first"
    )


def load(args) -> tuple[Config, State]:
    root = Path(args.root).resolve() if getattr(args, "root", None) else find_root(Path.cwd())
    cfg = load_config(root)
    state = load_state(cfg.state_path)
    return cfg, state


def guard_role(command: str) -> None:
    """Role sessions may read state, never change it."""
    role = os.environ.get(ROLE_ENV)
    if role and command not in READ_ONLY_FOR_ROLE:
        raise SystemExit(
            f"`hv {command}` is not available to a {role} session: role sessions "
            "change the world by writing artifacts, not by driving the dispatcher"
        )


# --------------------------------------------------------------------- init
TEMPLATES_DIR = SKILL_ROOT / "templates"
PROJECT_TEMPLATE = TEMPLATES_DIR / "project" / "hv.toml"
SPEC_TEMPLATE_DIR = TEMPLATES_DIR / "spec"


def render_template(text: str, values: dict) -> str:
    """Fill ``{{name}}`` placeholders from a flat mapping.

    Deliberately not ``str.format``: the templates contain shell and TOML
    braces that must survive untouched.
    """
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def spec_skeleton() -> dict[str, str]:
    """Read the spec-branch skeleton from ``templates/spec`` into rel-path -> text."""
    if not SPEC_TEMPLATE_DIR.is_dir():
        raise SystemExit(f"spec templates missing: {SPEC_TEMPLATE_DIR}")
    files: dict[str, str] = {}
    for template in sorted(SPEC_TEMPLATE_DIR.rglob("*")):
        if template.is_file():
            relative = template.relative_to(SPEC_TEMPLATE_DIR).as_posix()
            files[relative] = template.read_text(encoding="utf-8")
    return files


def cmd_init(args) -> int:
    root = Path(args.root or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / CONFIG_NAME).exists() and not args.force:
        raise SystemExit(f"{root / CONFIG_NAME} already exists (use --force to overwrite)")
    if not args.adapter_cmd:
        raise SystemExit(
            "hv init requires --adapter-cmd: there is no default executor.\n"
            "Example (the template must contain {prompt_file}):\n"
            "  --adapter-cmd 'hermes -z \"$(cat {prompt_file})\" --usage-file {usage_file}'\n"
            "More command lines: references/protocol.md, section 12."
        )

    gitops.ensure_repo(root, init=True)
    cfg_path = root / CONFIG_NAME
    cfg_path.write_text(
        render_template(
            PROJECT_TEMPLATE.read_text(encoding="utf-8"),
            {
                "name": args.name or root.name,
                "adapter_cmd": args.adapter_cmd.replace('"', '\\"'),
                "isolation": args.isolation,
            },
        ),
        encoding="utf-8",
    )
    cfg = load_config(root)

    gitops.create_orphan_branch(root, "spec", spec_skeleton(), "hv: spec skeleton")
    gitops.setup_excludes(root, ["run/", "worktrees/", ".hv/", ".spec/"])

    spec_wt = cfg.spec_worktree
    gitops.ensure_spec_worktree(root, cfg.spec_branch, spec_wt)
    gitops.ensure_run_dirs(cfg)
    gitignore = root / ".gitignore"
    entries = ["run/", "worktrees/", ".hv/", ".spec/"]
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    missing = [entry for entry in entries if entry not in existing]
    if missing:
        with gitignore.open("a", encoding="utf-8") as handle:
            if existing and existing[-1] != "":
                handle.write("\n")
            for entry in missing:
                handle.write(f"{entry}\n")
        gitops.git(root, "add", ".gitignore")
        gitops.commit_all(root, "hv: ignore dispatcher paths")

    save_state(cfg.state_path, new_state(cfg.name))
    log_event(cfg.log_path, "init", project=cfg.name, isolation=args.isolation)
    echo(f"initialised hv project `{cfg.name}` in {root}")
    echo("next: hv run --until blocked")
    return 0


# ------------------------------------------------------------------ commands
def cmd_status(args) -> int:
    cfg, state = load(args)
    if args.json:
        echo(report_mod.status_json(cfg, state))
    else:
        echo(report_mod.status_text(cfg, state))
    return 0


def cmd_plan(args) -> int:
    cfg, state = load(args)
    plan = plan_mod.load_plan(cfg.spec_worktree / plan_mod.PLAN_FILE)
    problems = plan_mod.validate_plan(plan)
    for increment in plan.increments:
        status = f"accepted in cycle {increment.accepted_in:02d}" if increment.accepted_in else "open"
        echo(f"{increment.id:8} v{increment.criteria_version} {status:24} "
             f"sha {increment.plan_sha()[:12]}  {increment.title}")
    if problems:
        echo("")
        echo("problems:")
        for problem in problems:
            echo(f"  ! {problem}")
    return 0


def cmd_run(args) -> int:
    guard_role("run")
    cfg, state = load(args)
    dispatcher = Dispatcher(cfg, state, echo=echo)
    stage = dispatcher.run(until=args.until, max_cycles=args.max_cycles)
    echo("")
    echo(report_mod.status_text(cfg, dispatcher.state))
    return 0 if stage not in ("BLOCKED",) else 2


def cmd_spawn(args) -> int:
    guard_role("spawn")
    cfg, state = load(args)
    dispatcher = Dispatcher(cfg, state, echo=echo)
    role = args.role
    writable = role in ("architect", "enabling")
    prompt = dispatcher.build_prompt(role, mode="accept" if role == "architect" else None)
    result = dispatcher.run_role(role, prompt, writable_spec=writable)
    echo(f"{role} session finished: exit={result.exit_code} timeout={result.timed_out} "
         f"{result.duration_s}s (log {result.log_path})")
    return 0 if result.ok else 1


def cmd_gate(args) -> int:
    cfg, state = load(args)
    dispatcher = Dispatcher(cfg, state, echo=echo)
    workdir = Path(state["worktree"]) if state.get("worktree") else cfg.root
    echo(f"gates in {workdir}")
    results = dispatcher.run_gate_set(("setup", "build", "test", "lint"),
                                      cold_setup=args.cold, workdir=workdir)
    failed = [name for name, res in results.items() if res.status not in ("pass", "skip")]
    for name, result in results.items():
        echo(f"{name:6} {result.status:8} exit={result.exit_code} {result.duration_s}s"
             + (" (cold)" if result.cold else ""))
        if result.status not in ("pass", "skip"):
            echo("  " + result.tail.strip().splitlines()[-1] if result.tail.strip() else "")
    return 0 if not failed else 1


def cmd_report(args) -> int:
    cfg, state = load(args)
    echo(report_mod.cycle_report(cfg, state, args.cycle or state.cycle))
    return 0


def cmd_log(args) -> int:
    cfg, _ = load(args)
    path = cfg.log_path
    if not path.exists():
        echo("no log yet")
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    tail = lines[-args.tail:] if args.tail else lines
    for line in tail:
        echo(line)
    return 0


def cmd_decide(args) -> int:
    guard_role("decide")
    cfg, state = load(args)
    decision = decisions.append_decision(
        cfg.spec_worktree,
        kind=args.kind,
        text=args.text,
        actor=args.actor or "human",
        cycle=args.cycle if args.cycle is not None else state.cycle,
        plan_sha=state.get("plan_sha") or "",
    )
    gitops.commit_all(cfg.spec_worktree, f"decisions: {decision.id} ({args.kind})")
    echo(f"{decision.id} written to DECISIONS.md and committed on `{cfg.spec_branch}`")
    return 0


def _require_decision(cfg, decision_id: str):
    decision = decisions.find_decision(cfg.spec_worktree, decision_id)
    if decision is None:
        raise SystemExit(
            f"decision {decision_id} not found in DECISIONS.md — write it first "
            f"(`hv decide --kind ... --text \"...\"`), then repeat the command"
        )
    return decision


def cmd_resolve(args) -> int:
    guard_role("resolve")
    cfg, state = load(args)
    if state.stage != "AWAITING_HUMAN":
        raise SystemExit(f"nothing to resolve: stage is {state.stage}")
    _require_decision(cfg, args.decision)
    awaiting = state.get("awaiting") or {}
    state["awaiting"] = None
    if args.stage:
        state.stage = args.stage
    else:
        state.stage = "IDLE"
    state["history"].append(
        {"ts": now(), "from": "AWAITING_HUMAN", "to": state.stage,
         "why": f"resolved by {args.decision}", "cycle": state.cycle,
         "phase": state.get("phase_id")}
    )
    log_event(cfg.log_path, "resolved", decision=args.decision, to=state.stage,
              reason=awaiting.get("reason"))
    state.save()
    echo(f"resolved with {args.decision}; stage is now {state.stage}")
    return 0


def cmd_reopen(args) -> int:
    guard_role("reopen")
    cfg, state = load(args)
    _require_decision(cfg, args.decision)
    plan = plan_mod.load_plan(cfg.spec_worktree / plan_mod.PLAN_FILE)
    increment = plan.get(state.get("phase_id"))
    if increment is None:
        raise SystemExit(f"increment {state.get('phase_id')} not found in PLAN.md")
    if args.criteria is not None:
        if int(increment.criteria_version) < int(args.criteria):
            raise SystemExit(
                f"PLAN.md still has criteria v{increment.criteria_version}; bump it to "
                f"v{args.criteria} before reopening (a version is a new version, never an edit)"
            )
        state["criteria_version"] = increment.criteria_version
    state.reset_counters(reset_total=False)
    state["reopened"] = True
    state["reopen_count"] = int(state.get("reopen_count") or 0) + 1
    state["plan_sha"] = increment.plan_sha()
    state["criteria_snapshot"] = increment.criteria_snapshot()
    state["attempt"]["dev"] = 0
    state["attempt"]["qa"] = 0
    state.stage = "DEV"
    state["awaiting"] = None
    state.save()
    log_event(cfg.log_path, "reopen", decision=args.decision, criteria=state.get("criteria_version"))
    echo(f"cycle {state.cycle:02d} reopened against criteria v{state.get('criteria_version')}; "
         "dev and QA will re-run")
    return 0


def cmd_approve(args) -> int:
    guard_role("approve")
    cfg, state = load(args)
    _require_decision(cfg, args.decision)
    if state.stage != "ACCEPT":
        raise SystemExit(f"nothing to approve: stage is {state.stage} (only ACCEPT can be approved)")
    state["awaiting"] = None
    state.save()
    echo("approved — `hv run` will merge, run the regression gate and tag the cycle")
    return 0


def cmd_reject(args) -> int:
    guard_role("reject")
    cfg, state = load(args)
    _require_decision(cfg, args.decision)
    cycle = args.cycle or state.cycle
    branch = state.get("branch") or f"cycle/{cycle:02d}"
    if gitops.ref_exists(cfg.root, branch):
        gitops.create_tag(cfg.root, f"cycle-{cycle:02d}-abandoned",
                          f"cycle {cycle:02d} abandoned: {args.reason or args.decision}")
        worktree = state.get("worktree")
        if worktree and Path(worktree).exists():
            gitops.worktree_remove(cfg.root, Path(worktree))
        gitops.delete_branch(cfg.root, branch)
    state["awaiting"] = None
    state.stage = "IDLE"
    state["branch"] = None
    state["worktree"] = None
    state.save()
    log_event(cfg.log_path, "cycle_rejected", cycle=cycle, decision=args.decision,
              reason=args.reason)
    echo(f"cycle {cycle:02d} tagged abandoned, branch deleted; commits survive under the tag")
    return 0


# ------------------------------------------------------------------- rebuild
def cmd_rebuild(args) -> int:
    guard_role("rebuild")
    cfg = load_config(Path(args.root).resolve() if args.root else find_root(Path.cwd()))
    gitops.ensure_spec_worktree(cfg.root, cfg.spec_branch, cfg.spec_worktree)
    gitops.ensure_run_dirs(cfg)
    gitops.setup_excludes(cfg.root, ["run/", "worktrees/", ".hv/", ".spec/"])
    plan = plan_mod.load_plan(cfg.spec_worktree / plan_mod.PLAN_FILE)
    accepted = [
        int(tag.split("-")[1]) for tag in
        gitops.git(cfg.root, "tag", "--list", "cycle-*-accepted").stdout.split()
        if tag.startswith("cycle-") and "-accepted" in tag and tag.split("-")[1].isdigit()
    ]
    last_accepted = max(accepted) if accepted else 0
    branches = [
        line.strip() for line in
        gitops.git(cfg.root, "branch", "--list", "cycle/*", "--format=%(refname:short)").stdout.splitlines()
        if line.strip()
    ]

    open_cycles: list[tuple[int, str]] = []
    for branch in branches:
        suffix = branch.split("/", 1)[1]
        number = "".join(ch for ch in suffix.split("-")[0] if ch.isdigit())
        if number:
            open_cycles.append((int(number), branch))
    open_cycles.sort()

    state = new_state(cfg.name)
    state["env_sha"] = gitops.env_sha(cfg.spec_worktree, cfg.spec_branch)

    if open_cycles:
        cycle, branch = open_cycles[-1]
        worktree = cfg.worktrees_dir / f"cycle-{cycle:02d}"
        for path in gitops.worktree_list(cfg.root):
            if path.get("branch", "").endswith(branch):
                worktree = Path(path["worktree"])
        state["cycle"] = cycle
        state["branch"] = branch
        state["worktree"] = str(worktree)
        reports_dir = worktree / cfg.reports_rel
        increment = plan.next_increment()
        if increment is None:
            increment = plan.increments[-1]
        stage = "DEV"
        dev_attempts = qa_attempts = 0
        for path in sorted(reports_dir.glob("STAGE-*attempt-*.md")) if reports_dir.exists() else []:
            dev_attempts = max(dev_attempts, _attempt_of(path))
        for path in sorted(reports_dir.glob("QA-*attempt-*.md")) if reports_dir.exists() else []:
            qa_attempts = max(qa_attempts, _attempt_of(path))
        if qa_attempts:
            stage = "ACCEPT"
            latest = reports_dir / f"QA-{cycle:02d}-attempt-{qa_attempts}.md"
            if latest.exists():
                body = latest.read_text(encoding="utf-8").lower()
                if "| fail" in body or ": fail" in body:
                    stage = "DEV"
        elif dev_attempts:
            stage = "QA" if (reports_dir / f"STAGE-{cycle:02d}-attempt-{dev_attempts}.md").exists() else "DEV"
        state["stage"] = stage
        state["attempt"] = {"env": 1 if state["env_sha"] else 0, "dev": dev_attempts,
                            "qa": qa_attempts, "architect": 0}
        notes = [f"resumed open branch {branch}", f"stage inferred as {stage} from artifacts"]
    else:
        state["cycle"] = last_accepted
        state["stage"] = "IDLE" if last_accepted else "IDLE"
        notes = [f"no open cycle branch; last accepted cycle {last_accepted}"]

    increment = plan.next_increment()
    if increment:
        state["phase_id"] = increment.id
        state["plan_sha"] = increment.plan_sha()
        state["criteria_version"] = increment.criteria_version
        state["criteria_snapshot"] = increment.criteria_snapshot()
    state["plan_ver"] = plan.plan_ver
    state["gates"] = {}
    state["gate_details"] = {}
    state["rebuild_notes"] = notes
    state["isolation"] = "none"
    save_state(cfg.state_path, state)
    log_event(cfg.log_path, "rebuild", cycle=state["cycle"], stage=state["stage"],
              notes=notes)
    echo(f"STATE rebuilt: cycle {state['cycle']:02d}, stage {state['stage']}")
    for note in notes:
        echo(f"  · {note}")
    echo("gates are not part of STATE rebuild — run `hv gate` to reproduce them")
    return 0


def _attempt_of(path: Path) -> int:
    try:
        return int(path.stem.rsplit("-attempt-", 1)[1])
    except (IndexError, ValueError):
        return 0


def cmd_version(args) -> int:
    echo("hv 0.1.0 (agent-hypervisor protocol v1)")
    return 0


# ---------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hv",
        description="Dispatcher for the agent-hypervisor protocol: one state "
                    "machine, role sessions as child processes, artifacts as state.",
    )
    parser.add_argument("--root", help="project root (default: nearest hv.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create hv.toml, the spec branch and run dirs")
    init.add_argument("--name")
    init.add_argument("--adapter-cmd", help="command template with {prompt_file}")
    init.add_argument("--isolation", default="docker", choices=["docker", "none"])
    init.add_argument("--root")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="where we are, what is blocked")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    plan_cmd = sub.add_parser("plan", help="increments and their pins")
    plan_cmd.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="drive transitions until a significant boundary")
    run.add_argument("--until", default="blocked",
                     choices=["blocked", "cycle", "accept", "done"])
    run.add_argument("--max-cycles", type=int, default=0)
    run.set_defaults(func=cmd_run)

    spawn = sub.add_parser("spawn", help="one role session: render, run, collect")
    spawn.add_argument("role", choices=["architect", "enabling", "dev", "qa"])
    spawn.set_defaults(func=cmd_spawn)

    gate = sub.add_parser("gate", help="run the deterministic gates")
    gate.add_argument("--cold", action="store_true", help="run setup/test in a cold container")
    gate.set_defaults(func=cmd_gate)

    report = sub.add_parser("report", help="fixed-shape cycle report")
    report.add_argument("cycle", nargs="?", type=int)
    report.set_defaults(func=cmd_report)

    log = sub.add_parser("log", help="append-only dispatcher log")
    log.add_argument("--tail", type=int, default=0)
    log.set_defaults(func=cmd_log)

    decide = sub.add_parser("decide", help="append a decision to DECISIONS.md")
    decide.add_argument("--kind", required=True,
                        choices=["accept", "reject", "reopen", "criteria-change", "note", "env"])
    decide.add_argument("--text", required=True)
    decide.add_argument("--actor", default="human")
    decide.add_argument("--cycle", type=int)
    decide.set_defaults(func=cmd_decide)

    resolve = sub.add_parser("resolve", help="leave AWAITING_HUMAN (needs a decision id)")
    resolve.add_argument("--decision", required=True)
    resolve.add_argument("--stage", choices=["IDLE", "PLANNING", "ENV", "DEV", "QA", "ACCEPT", "DONE"])
    resolve.set_defaults(func=cmd_resolve)

    reopen = sub.add_parser("reopen", help="re-run dev/QA against a new criteria version")
    reopen.add_argument("--criteria", type=int)
    reopen.add_argument("--decision", required=True)
    reopen.add_argument("--reason")
    reopen.set_defaults(func=cmd_reopen)

    approve = sub.add_parser("approve", help="accept the current cycle")
    approve.add_argument("cycle", nargs="?", type=int)
    approve.add_argument("--decision", required=True)
    approve.add_argument("--reason")
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject", help="abandon the cycle: tag abandoned, drop the branch")
    reject.add_argument("cycle", nargs="?", type=int)
    reject.add_argument("--decision", required=True)
    reject.add_argument("--reason")
    reject.set_defaults(func=cmd_reject)

    rebuild = sub.add_parser("rebuild", help="re-derive STATE from git artifacts")
    rebuild.add_argument("--root")
    rebuild.set_defaults(func=cmd_rebuild)

    version = sub.add_parser("version", help="print the protocol version")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, StateError, DispatcherError,
            gitops.GitError, plan_mod.PlanError) as exc:
        echo(f"hv: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
