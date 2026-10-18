"""The dispatcher: deterministic transitions, roles supply judgement.

Everything in this module is a rule from the protocol, not an opinion. A role
session may only produce files; the dispatcher decides what happens next.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import artifacts, gates, gitops, plan as plan_mod, prompts
from .adapter import run_session
from .config import ATTEMPT_KEY, Config, attempt_key
from .state import State, log_event, now

__all__ = ["Dispatcher", "STOP_STAGES", "STAGE_ROLE"]

STOP_STAGES = ("AWAITING_HUMAN", "BLOCKED", "DONE")

STAGE_ROLE = {
    "PLANNING": "architect",
    "ENV": "enabling",
    "DEV": "dev",
    "QA": "qa",
    "ACCEPT": "architect",
}

SPEC_WRITABLE_STAGES = ("PLANNING", "ACCEPT", "ENV")


class DispatcherError(RuntimeError):
    """The project is in a state the dispatcher cannot advance."""


class Dispatcher:
    def __init__(self, cfg: Config, state: State, *, echo=None):
        self.cfg = cfg
        self.state = state
        self.skill_root = Path(__file__).resolve().parents[2]
        self._echo = echo or (lambda message: None)
        self.isolation, self.isolation_warning = gates.isolation_status(cfg)
        if self.isolation_warning:
            self._echo(f"warning: {self.isolation_warning}; running without isolation")
        self.state["isolation"] = self.isolation

    # ------------------------------------------------------------------ util
    def say(self, message: str) -> None:
        self._echo(message)

    def log(self, event: str, **fields) -> None:
        log_event(self.cfg.log_path, event, **fields)

    def transition(self, to: str, why: str) -> None:
        from_stage = self.state.stage
        self.state.stage = to
        self.state["history"].append(
            {"ts": now(), "from": from_stage, "to": to, "why": why,
             "cycle": self.state.cycle, "phase": self.state.get("phase_id")}
        )
        self.log("transition", **{"from": from_stage, "to": to, "why": why,
                                  "cycle": self.state.cycle})
        self.say(f"[{self.state.cycle:02d}/{self.state.get('phase_id') or '-'}] "
                 f"{from_stage} → {to}: {why}")
        self.state.save()

    def await_human(self, reason: str, options: list[str]) -> None:
        self.state["awaiting"] = {"reason": reason, "options": options}
        self.transition("AWAITING_HUMAN", reason)

    def block(self, reason: str) -> None:
        self.state["awaiting"] = {"reason": reason, "options": ["hv rebuild", "hv status"]}
        self.transition("BLOCKED", reason)

    # ----------------------------------------------------------------- paths
    @property
    def spec(self) -> Path:
        return self.cfg.spec_worktree

    @property
    def worktree(self) -> Path:
        worktree = self.state.get("worktree")
        if not worktree:
            raise DispatcherError("STATE has no worktree; run `hv rebuild`")
        return Path(worktree)

    def plan(self) -> plan_mod.Plan:
        return plan_mod.load_plan(self.spec / plan_mod.PLAN_FILE)

    def increment(self, plan: plan_mod.Plan | None = None) -> plan_mod.Increment:
        plan = plan or self.plan()
        phase = self.state.get("phase_id")
        if not phase:
            raise DispatcherError("STATE has no phase_id")
        increment = plan.get(phase)
        if increment is None:
            raise DispatcherError(f"increment {phase} not found in PLAN.md")
        return increment

    def report(self, kind: str, attempt: int, role: str | None = None) -> Path:
        return artifacts.report_path(
            self.worktree, self.cfg.reports_rel, kind, self.state.cycle, attempt, role
        )

    # ------------------------------------------------------------- workspace
    def materialize_spec(self, writable: bool = False) -> Path:
        return gitops.materialize_spec(
            self.spec, self.cfg.spec_branch, self.worktree / self.cfg.path("spec").name,
            writable=writable,
        )

    def spec_changes(self) -> list[str]:
        """Files changed in the materialised ``.spec`` compared to the branch."""
        dest = self.worktree / self.cfg.path("spec").name
        changed: list[str] = []
        if not dest.exists():
            return changed
        for path in sorted(dest.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(dest)
            source = self.spec / rel
            if not source.exists():
                changed.append(str(rel))
                continue
            if path.read_bytes() != source.read_bytes():
                changed.append(str(rel))
        return changed

    def commit_spec_changes(self, message: str, allowed: tuple[str, ...]) -> list[str]:
        """Copy allowed spec changes back and commit them on the spec branch."""
        changed = self.spec_changes()
        if not changed:
            return []
        illegal = [
            rel for rel in changed
            if not any(rel == prefix or rel.startswith(prefix.rstrip("/") + "/") for prefix in allowed)
        ]
        if illegal:
            raise DispatcherError(
                "session edited files it does not own on the spec branch: "
                + ", ".join(illegal)
            )
        dest = self.worktree / self.cfg.path("spec").name
        for rel in changed:
            source = dest / rel
            target = self.spec / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        gitops.commit_all(self.spec, message)
        return changed

    def spec_untouched(self, stage: str) -> list[str]:
        """A non-owner stage must not change the spec copy at all."""
        changed = self.spec_changes()
        if changed and stage not in SPEC_WRITABLE_STAGES:
            return [
                f"spec branch was edited during stage {stage}: " + ", ".join(changed)
            ]
        return []

    # ---------------------------------------------------------------- cycles
    def start_cycle(self) -> str:
        plan = None
        try:
            plan = self.plan()
        except plan_mod.PlanError:
            pass

        increment = plan.next_increment() if plan else None
        if plan is not None and increment is None:
            self.transition("DONE", "no increment left in PLAN.md")
            return "stop"

        cycle = self.state.cycle + 1
        branch = f"cycle/{cycle:02d}"
        worktree = self.cfg.worktrees_dir / f"cycle-{cycle:02d}"
        if gitops.ref_exists(self.cfg.root, branch):
            self.block(f"branch {branch} already exists; rebuild or pick another cycle")
            return "stop"
        gitops.worktree_add(self.cfg.root, worktree, branch=branch, from_ref=self.cfg.main_branch)
        gitops.setup_excludes(
            self.cfg.root,
            [f"{self.cfg.path('run').name}/", f"{self.cfg.path('worktrees').name}/",
             f"{self.cfg.path('hv').name}/", f"{self.cfg.path('spec').name}/"],
        )

        self.state["cycle"] = cycle
        self.state["branch"] = branch
        self.state["worktree"] = str(worktree)
        self.state["attempt"] = {"env": 0, "dev": 0, "architect": 0, "qa": 0}
        self.state.reset_counters()
        self.state["gates"] = {}
        self.state["gate_details"] = {}
        self.state["reopened"] = False
        self.state["awaiting"] = None
        self.state["plan_sha"] = None
        self.state["phase_id"] = increment.id if increment else None
        self.state.save()
        self.log("cycle_start", cycle=cycle, branch=branch, phase=self.state.get("phase_id"))
        self.transition("PLANNING", f"cycle {cycle} opened on {branch}")
        return "continue"

    def _next_worktree(self, suffix: str) -> tuple[str, Path]:
        cycle = self.state.cycle
        branch = f"cycle/{cycle:02d}-{suffix}"
        worktree = self.cfg.worktrees_dir / f"cycle-{cycle:02d}-{suffix}"
        return branch, worktree

    # --------------------------------------------------------------- session
    def run_role(self, role: str, prompt: str, *, writable_spec: bool = False,
                 attempt: int | None = None) -> object:
        self.materialize_spec(writable=writable_spec)
        key = attempt_key(role)
        attempt = int(attempt or (int(self.state["attempt"].get(key, 0)) + 1))
        self.state["attempt"][key] = attempt
        self.state.save()
        self.say(f"  · {role} session #{attempt} (isolation={self.isolation})")
        pins = {
            "plan_sha": str(self.state.get("plan_sha") or ""),
            "criteria_version": int(self.state.get("criteria_version") or 0),
            "increment": str(self.state.get("phase_id") or ""),
            "stage": self.state.stage,
        }
        result = run_session(
            self.cfg, role, prompt, self.worktree, self.state.cycle, attempt, pins=pins
        )
        self.record_cost(result)
        self.log("session", role=role, attempt=attempt, exit=result.exit_code,
                 timed_out=result.timed_out, seconds=result.duration_s,
                 usage=result.usage or None)
        self.wip_commit(role, attempt)
        return result

    def record_cost(self, result) -> None:
        entry = {
            "role": result.role,
            "cycle": result.cycle,
            "attempt": result.attempt,
            "seconds": result.duration_s,
            "exit": result.exit_code,
            "timed_out": result.timed_out,
            **{key: value for key, value in (result.usage or {}).items()},
        }
        self.state["cost"]["sessions"].append(entry)
        cost = result.usage.get("cost_usd") if result.usage else None
        if isinstance(cost, (int, float)):
            self.state["cost"]["total_usd"] = round(
                float(self.state["cost"].get("total_usd") or 0) + float(cost), 4
            )
        self.state.save()

    def wip_commit(self, role: str, attempt: int) -> None:
        message = f"wip/{role}-c{self.state.cycle:02d}-a{attempt}"
        gitops.commit_all(self.worktree, message)

    def session_failed(self, result, role: str) -> str | None:
        """Record a crash: a session that did not reach an artifact."""
        if result.ok:
            return None
        key = "env" if role == "enabling" else role
        crashes = self.state.bump("crashes", key)
        limit = int(self.cfg.limits["crashes"])
        detail = "timeout" if result.timed_out else f"exit {result.exit_code}"
        self.log("session_crash", role=role, crashes=crashes, detail=detail,
                 timed_out=result.timed_out, exit=result.exit_code)
        if crashes > limit:
            self.block(f"{role} session crashed {crashes}x (limit {limit}): {detail}")
            return "stop"
        self.say(f"  ! {role} session failed ({detail}); retrying once")
        return "retry"

    def artifact_problems(self, role: str, problems: list[str], stage: str) -> str:
        """One retry with the defect listed, then a human decides."""
        retries = self.state.bump("artifact_retries", role)
        detail = "; ".join(problems)
        self.log("artifact_rejected", role=role, problems=problems, retries=retries)
        self.say(f"  ! artifact rejected: {detail}")
        self.write_retry_note(role, problems, stage)
        if retries > 1:
            self.await_human(
                f"{role} artifact is still invalid after a retry: {detail}",
                ["hv spawn <role>", "hv reject <cycle> --decision D-NNN --reason ..."],
            )
            return "stop"
        return "retry"

    def write_retry_note(self, role: str, problems: list[str], stage: str) -> None:
        """Leave the defect list where the next attempt's prompt will find it."""
        if role not in ("dev", "qa", "enabling"):
            return
        attempt = int(self.state["attempt"].get(attempt_key(role), 0))
        target = self.report("stage-report", attempt).parent / (
            f"RETRY-{role}-{self.state.cycle:02d}-attempt-{attempt}.md"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        body = [
            f"# Artifact rejected · {role} · cycle {self.state.cycle:02d} attempt {attempt}",
            "",
            f"- stage: {stage}",
            f"- plan_sha: {self.state.get('plan_sha') or ''}",
            f"- criteria_version: {self.state.get('criteria_version') or ''}",
            "",
            "## Problems",
            "",
            *[f"- {problem}" for problem in problems],
            "",
        ]
        target.write_text("\n".join(body), encoding="utf-8")
        gitops.commit_all(self.worktree, f"retry/{role}-c{self.state.cycle:02d}-a{attempt}")

    # ---------------------------------------------------------------- stages
    def step(self) -> str:
        stage = self.state.stage
        if stage == "IDLE":
            return self.start_cycle()
        if stage == "PLANNING":
            return self.stage_planning()
        if stage == "ENV":
            return self.stage_env()
        if stage == "DEV":
            return self.stage_dev()
        if stage == "QA":
            return self.stage_qa()
        if stage == "ACCEPT":
            return self.stage_accept()
        return "stop"

    def _drift_check(self) -> str | None:
        """Detect plan/criteria changes made outside the allowed boundary."""
        try:
            plan = self.plan()
        except plan_mod.PlanError as exc:
            self.await_human(f"PLAN.md unreadable: {exc}", ["hv status", "hv rebuild"])
            return "stop"
        try:
            increment = self.increment(plan)
        except DispatcherError as exc:
            self.block(str(exc))
            return "stop"
        current_sha = increment.plan_sha()
        if increment.criteria_version != int(self.state.get("criteria_version") or 0):
            self.state.reset_counters(reset_total=False)
            self.state["reopened"] = True
            self.state["reopen_count"] = int(self.state.get("reopen_count") or 0) + 1
            self.state["criteria_version"] = increment.criteria_version
            self.state["plan_sha"] = current_sha
            self.state["criteria_snapshot"] = increment.criteria_snapshot()
            self.log("reopened", criteria_version=increment.criteria_version)
            self.transition(
                "PLANNING",
                f"criteria v{increment.criteria_version} changed mid-cycle — reopen",
            )
            return "reopened"
        if self.state.get("plan_sha") and current_sha != self.state["plan_sha"]:
            self.await_human(
                "PLAN.md changed mid-cycle without a criteria version bump — "
                "frozen plan violated (plan_sha "
                f"{self.state['plan_sha'][:12]} → {current_sha[:12]})",
                ["hv reopen --criteria <v> --decision D-NNN --reason ...", "hv rebuild"],
            )
            return "stop"
        return None

    def stage_planning(self) -> str:
        plan = None
        need_session = bool(self.state.get("plan_requested"))
        try:
            plan = self.plan()
        except plan_mod.PlanError:
            plan = None
            need_session = True
        if plan is not None:
            increment = plan.next_increment()
            if increment is None or not increment.acceptance:
                need_session = True

        if need_session:
            self.state["plan_requested"] = False
            prompt = self.build_prompt("architect", plan=plan)
            result = self.run_role("architect", prompt, writable_spec=True)
            failed = self.session_failed(result, "architect")
            if failed == "stop":
                return "stop"
            if failed == "retry":
                return self.stage_planning()
            self.commit_spec_changes(
                f"plan: cycle {self.state.cycle:02d}",
                allowed=("PLAN.md", "DECISIONS.md", "roles/"),
            )
            try:
                plan = self.plan()
            except plan_mod.PlanError as exc:
                self.await_human(f"PLAN.md is not parseable after planning: {exc}",
                                 ["hv spawn architect", "hv status"])
                return "stop"

        increment = plan.next_increment() if plan is not None else None
        if increment is None:
            self.await_human(
                "no increment is ready (the plan is empty, all increments are accepted, "
                "or dependencies are unmet)",
                ["add an increment to PLAN.md", "hv reject <cycle> --decision D-NNN"],
            )
            return "stop"

        problems = plan_mod.validate_plan(plan)
        if problems:
            outcome = self.artifact_problems("architect", problems, "PLANNING")
            if outcome == "retry":
                return self.stage_planning()
            return "stop"

        self.state["phase_id"] = increment.id
        previous_version = int(self.state.get("criteria_version") or 0)
        self.state["plan_sha"] = increment.plan_sha()
        self.state["plan_ver"] = plan.plan_ver
        self.state["criteria_version"] = increment.criteria_version
        self.state["criteria_snapshot"] = increment.criteria_snapshot()
        if previous_version and increment.criteria_version != previous_version:
            # a plan revision against new criteria restarts dev/QA, but the
            # per-increment rejection fuse is cumulative on purpose
            self.state.reset_counters(reset_total=False)
            self.state["reopened"] = True
            self.state["reopen_count"] = int(self.state.get("reopen_count") or 0) + 1
            self.log("reopened_by_plan", criteria_version=increment.criteria_version)
        else:
            self.state["reopened"] = False
        self.state.save()
        self.log("planning", phase=increment.id, plan_sha=self.state["plan_sha"],
                 criteria_version=increment.criteria_version)

        if self.state.get("env_sha"):
            self.transition("DEV", f"increment {increment.id} frozen, environment present")
        else:
            self.transition("ENV", "no accepted environment for the project")
        return "continue"

    def stage_env(self) -> str:
        drift = self._drift_check()
        if drift:
            return "stop" if drift == "stop" else "continue"

        increment = self.increment()
        attempts = int(self.state.counters["env_attempts"]) + 1
        self.state.counters["env_attempts"] = attempts
        self.state.save()

        prompt = self.build_prompt("enabling", increment=increment)
        result = self.run_role("enabling", prompt, writable_spec=True)
        failed = self.session_failed(result, "enabling")
        if failed == "stop":
            return "stop"
        if failed == "retry":
            return self.stage_env()

        spec_dir = self.worktree / self.cfg.path("spec").name
        env_dir = spec_dir / "env"
        env_md = env_dir / "ENV.md"
        problems: list[str] = []
        if not env_md.exists() or not env_md.read_text(encoding="utf-8").strip():
            problems.append("env/ENV.md is missing or empty on the spec branch")
        if problems:
            outcome = self.artifact_problems("enabling", problems, "ENV")
            if outcome == "retry":
                return self.stage_env()
            return "stop"

        changed = self.commit_spec_changes(
            f"env: cycle {self.state.cycle:02d} attempt {attempts}", allowed=("env/",)
        )
        self.state["env_sha"] = gitops.env_sha(self.spec, self.cfg.spec_branch)
        self.state.save()
        if changed:
            self.log("env_updated", files=changed, env_sha=self.state["env_sha"])

        results = self.run_gate_set(("setup", "build", "test"), cold_setup=True)
        failures = [name for name, result in results.items() if result.status not in ("pass", "skip")]
        if failures:
            limit = int(self.cfg.limits["env_attempts"])
            detail = "; ".join(f"{name}: {results[name].status}" for name in failures)
            if attempts >= limit:
                self.await_human(
                    f"environment repair failed {attempts}x (limit {limit}): {detail}",
                    ["hv spawn enabling", "hv resolve AWAITING_HUMAN --decision D-NNN --choice retry"],
                )
                return "stop"
            self.log("env_broken", failures=failures, attempt=attempts)
            self.say(f"  ! environment gates failed ({detail}); repairing")
            return self.stage_env()

        self.transition("DEV", "environment accepted (setup/build/test)")
        return "continue"

    def run_gate_set(self, names: tuple[str, ...], *, cold_setup: bool = False,
                     workdir: Path | None = None) -> dict:
        workdir = Path(workdir or self.worktree)
        results: dict[str, gates.GateResult] = {}
        for name in names:
            cmd = self.cfg.gate_cmd(name)
            if not cmd:
                continue
            cold = cold_setup and name == "setup"
            result = gates.run_gate(self.cfg, name, cmd, workdir, cold=cold)
            results[name] = result
            self.state["gates"][name] = result.to_state()
            self.state["gate_details"][name] = result.as_dict()
        self.state.save()
        return results

    def _manifest_hits(self) -> list[str]:
        changed = gitops.changed_files(self.worktree, self.cfg.main_branch)
        patterns = self.cfg.manifest_patterns
        hits = []
        for path in changed:
            name = Path(path).name
            for pattern in patterns:
                if Path(name).match(pattern) or path == pattern:
                    hits.append(path)
                    break
        return hits

    def _reject_dev(self, reasons: list[str], detail: str = "") -> str:
        attempt = int(self.state["attempt"]["dev"])
        rejections = self.state.bump("rejections", "dev")
        total = self.state.bump_total()
        note = self.report("stage-report", attempt)
        reject_path = note.parent / f"REJECT-{self.state.cycle:02d}-attempt-{attempt}.md"
        reject_path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            f"# Rejection · cycle {self.state.cycle:02d} · dev attempt {attempt}",
            "",
            f"- plan_sha: {self.state.get('plan_sha') or ''}",
            f"- criteria_version: {self.state.get('criteria_version') or ''}",
            f"- rejections_dev: {rejections}",
            "",
            "## Reasons",
            "",
            *[f"- {reason}" for reason in reasons],
            "",
        ]
        if detail:
            body += ["## Gate output (tail)", "", "```", detail[-3000:], "```", ""]
        reject_path.write_text("\n".join(body), encoding="utf-8")
        gitops.commit_all(self.worktree, f"reject/cycle-{self.state.cycle:02d}-dev-a{attempt}")
        self.log("dev_rejected", reasons=reasons, rejections=rejections)

        total_limit = int(self.cfg.limits["total_rejections"])
        if total >= total_limit:
            self.await_human(
                f"{total} rejections in this increment (fuse {total_limit}) — the plan, "
                "not the code, needs attention",
                ["hv reopen --criteria <v> --decision D-NNN --reason ...", "hv reject <cycle>"],
            )
            return "stop"
        dev_limit = int(self.cfg.limits["dev_rejections"])
        if rejections >= dev_limit:
            self.state["plan_requested"] = True
            self.transition(
                "PLANNING",
                f"dev rejected {rejections}x (limit {dev_limit}) — plan review, not another patch",
            )
            return "continue"
        self.state.save()
        return "retry"

    def stage_dev(self) -> str:
        drift = self._drift_check()
        if drift:
            return "stop" if drift == "stop" else "continue"

        increment = self.increment()
        attempt = int(self.state["attempt"].get("dev", 0)) + 1
        prompt = self.build_prompt("dev", increment=increment, attempt=attempt)
        result = self.run_role("dev", prompt, attempt=attempt)
        failed = self.session_failed(result, "dev")
        if failed == "stop":
            return "stop"
        if failed == "retry":
            return self.stage_dev()

        problems = self.spec_untouched("DEV")
        if not problems:
            problems = self.validate_artifact("dev", artifacts.STAGE_REPORT, attempt, increment)
        if problems:
            outcome = self.artifact_problems("dev", problems, "DEV")
            if outcome == "retry":
                return self.stage_dev()
            return "stop"

        manifests = self._manifest_hits()
        if manifests:
            report = artifacts.parse_report(self.report(artifacts.STAGE_REPORT, attempt),
                                            artifacts.STAGE_REPORT)
            deviations = report.section("deviations").lower()
            unnamed = [path for path in manifests if Path(path).name.lower() not in deviations]
            if unnamed:
                outcome = self._reject_dev(
                    [f"manifest changed without an explicit deviation: {', '.join(unnamed)}"],
                    "the report's deviations section must name every changed manifest file",
                )
                return "stop" if outcome == "stop" else "continue"
            cold = gates.run_gate(
                self.cfg, "setup", self.cfg.gate_cmd("setup"), self.worktree, cold=True
            )
            self.state["gate_details"]["setup_cold"] = cold.as_dict()
            self.state["gates"]["setup"] = cold.to_state()
            self.state.save()
            if cold.status not in ("pass", "skip"):
                outcome = self._reject_dev(
                    [f"cold `make setup` failed after a manifest change ({cold.status})"],
                    cold.tail,
                )
                return "stop" if outcome == "stop" else "continue"

        results = self.run_gate_set(("build", "test", "lint"))
        failures = [name for name, res in results.items() if res.status not in ("pass", "skip")]
        if failures:
            detail = "; ".join(f"{name}: {results[name].status}" for name in failures)
            tail = "\n\n".join(
                f"### {name}\n{results[name].tail}" for name in failures
            )
            outcome = self._reject_dev([f"gate failed: {detail}"], tail)
            return "stop" if outcome == "stop" else "continue"

        gitops.commit_all(
            self.worktree,
            f"cycle {self.state.cycle:02d} inc {increment.id}: dev attempt {attempt}",
        )
        self.transition("QA", f"dev artifact accepted, gates green ({attempt} attempts)")
        return "continue"

    def validate_artifact(self, role: str, schema: str, attempt: int,
                          increment: plan_mod.Increment) -> list[str]:
        path = self.report(schema, attempt)
        try:
            report = artifacts.parse_report(path, schema)
        except artifacts.ArtifactError as exc:
            return [str(exc)]
        return artifacts.validate_report(
            report,
            increment,
            str(self.state.get("plan_sha") or ""),
            int(self.state.get("criteria_version") or 0),
        )

    def stage_qa(self) -> str:
        drift = self._drift_check()
        if drift:
            return "stop" if drift == "stop" else "continue"

        increment = self.increment()
        attempt = int(self.state["attempt"].get("qa", 0)) + 1
        prompt = self.build_prompt("qa", increment=increment, attempt=attempt)
        result = self.run_role("qa", prompt, attempt=attempt)
        failed = self.session_failed(result, "qa")
        if failed == "stop":
            return "stop"
        if failed == "retry":
            return self.stage_qa()

        problems = self.spec_untouched("QA")
        if not problems:
            problems = self.validate_artifact("qa", artifacts.QA_REPORT, attempt, increment)
        if problems:
            outcome = self.artifact_problems("qa", problems, "QA")
            if outcome == "retry":
                return self.stage_qa()
            return "stop"

        report = artifacts.parse_report(self.report(artifacts.QA_REPORT, attempt),
                                        artifacts.QA_REPORT)
        verdicts = artifacts.criteria_verdicts(report.body)
        failed_ids = [cid for cid, verdict in verdicts.items() if verdict == "fail"]
        if not failed_ids:
            self.state["gates"]["qa"] = f"pass {len(verdicts)}/{len(verdicts)}"
            self.state.save()
            self.transition("ACCEPT", f"QA passed all {len(verdicts)} criteria")
            return "continue"

        rejections = self.state.bump("rejections", "qa")
        total = self.state.bump_total()
        self.log("qa_failed", criteria=failed_ids, rejections=rejections)
        if len(failed_ids) != len(verdicts):
            self.state["gates"]["qa"] = f"fail {len(failed_ids)}/{len(verdicts)}"
        self.state.save()
        limit = int(self.cfg.limits["qa_rejections"])
        total_limit = int(self.cfg.limits["total_rejections"])
        if total >= total_limit:
            self.await_human(
                f"{total} rejections in this increment (fuse {total_limit})",
                ["hv reopen --criteria <v> --decision D-NNN", "hv reject <cycle>"],
            )
            return "stop"
        if rejections >= limit:
            self.state["plan_requested"] = True
            self.transition(
                "PLANNING",
                f"QA failed {rejections}x on {', '.join(failed_ids)} — plan revision, not a patch",
            )
            return "continue"
        self.transition("DEV", f"QA failed {', '.join(failed_ids)} (attempt {rejections})")
        return "continue"

    # ---------------------------------------------------------------- accept
    def stage_accept(self) -> str:
        drift = self._drift_check()
        if drift:
            return "stop" if drift == "stop" else "continue"

        increment = self.increment()
        qa_attempt = int(self.state["attempt"].get("qa", 0))
        if qa_attempt:
            problems = self.validate_artifact("qa", artifacts.QA_REPORT, qa_attempt, increment)
            if problems:
                self.await_human(
                    "QA artifact disagrees with the accepted plan: " + "; ".join(problems),
                    ["hv spawn qa", "hv reject <cycle> --decision D-NNN --reason ..."],
                )
                return "stop"
        attempt = int(self.state["attempt"].get("architect", 0)) + 1
        prompt = self.build_prompt("architect", increment=increment, mode="accept",
                                   attempt=attempt)
        result = self.run_role("architect", prompt, writable_spec=True, attempt=attempt)
        failed = self.session_failed(result, "architect")
        if failed == "stop":
            return "stop"
        if failed == "retry":
            return self.stage_accept()

        changed = self.commit_spec_changes(
            f"accept: cycle {self.state.cycle:02d} increment {increment.id}",
            allowed=("PLAN.md", "DECISIONS.md", "roles/", "env/"),
        )
        if changed:
            self.log("spec_updated", files=changed)

        plan = self.plan()
        current = plan.get(increment.id)
        problems: list[str] = []
        if current is None:
            problems.append(f"increment {increment.id} disappeared from PLAN.md")
        elif current.accepted_in != self.state.cycle:
            problems.append(
                f"increment {increment.id} is not marked `accepted_in: {self.state.cycle}`"
            )
        if problems:
            outcome = self.artifact_problems("architect", problems, "ACCEPT")
            if outcome == "retry":
                return self.stage_accept()
            return "stop"

        # criteria may not be weakened silently: any text/check change without
        # a version bump is an audit-trail defect.
        previous = self.state.get("criteria_snapshot") or {}
        for criterion in current.acceptance:
            before = previous.get(criterion.id)
            after = criterion.snapshot()
            if not before:
                problems.append(f"criterion {criterion.id} appeared without a plan revision")
                continue
            if before["version"] != after["version"] and after["version"] < before["version"]:
                problems.append(f"criterion {criterion.id} version went backwards")
            if (before["text_sha"] != after["text_sha"] or before["check_sha"] != after["check_sha"]) \
                    and before["version"] == after["version"]:
                problems.append(
                    f"criterion {criterion.id} was rewritten in place at v{after['version']}"
                )
        if problems:
            self.await_human(
                "acceptance criteria changed without a new version: " + "; ".join(problems),
                ["hv reopen --criteria <v> --decision D-NNN --reason ...", "hv reject <cycle>"],
            )
            return "stop"

        squash_message = (
            f"cycle {self.state.cycle:02d}: increment {increment.id} — {increment.title}"
        )
        pre_merge = gitops.head_sha(self.cfg.root, self.cfg.main_branch)
        self.squash_cycle(squash_message)
        ok, output = self.merge_cycle(squash_message)
        if not ok:
            self.block(f"merge of {self.state['branch']} into {self.cfg.main_branch} failed: {output[-800:]}")
            return "stop"

        results = self.run_gate_set(("build", "test", "lint"), workdir=self.cfg.root)
        failures = [name for name, res in results.items() if res.status not in ("pass", "skip")]
        if failures:
            detail = "; ".join(f"{name}: {results[name].status}" for name in failures)
            self.rollback_merge(pre_merge)
            self.block(f"regression gate failed on merged result: {detail}")
            return "stop"

        gitops.create_tag(
            self.cfg.root,
            f"cycle-{self.state.cycle:02d}-accepted",
            f"cycle {self.state.cycle:02d}: {increment.id} {increment.title}",
        )
        self.write_cost_report()
        self.state["gates"]["regression"] = "pass"
        self.state["reopened"] = False
        self.state["branch"] = None
        self.state["worktree"] = None
        self.state.save()
        self.log("cycle_accepted", cycle=self.state.cycle, phase=increment.id,
                 plan_sha=self.state.get("plan_sha"))
        self.transition("IDLE", f"cycle {self.state.cycle:02d} accepted and merged")
        return "continue"

    def squash_cycle(self, message: str) -> None:
        """Collapse WIP commits of the cycle into one reviewable commit."""
        main_sha = gitops.head_sha(self.cfg.root, self.cfg.main_branch)
        result = gitops.git(self.worktree, "reset", "--soft", main_sha)
        if not result.ok:
            raise DispatcherError(f"cannot squash cycle branch: {result}")
        gitops.commit_all(self.worktree, message)

    def merge_cycle(self, message: str) -> tuple[bool, str]:
        root = self.cfg.root
        branch_ref = gitops.git(root, "symbolic-ref", "--short", "HEAD")
        if branch_ref.stdout.strip() != self.cfg.main_branch:
            return False, (
                f"project root is on {branch_ref.stdout.strip() or 'detached HEAD'}, "
                f"not {self.cfg.main_branch}; the dispatcher merges only into a clean main checkout"
            )
        if gitops.is_dirty(root):
            return False, "project root has uncommitted changes; commit or stash before accepting"
        return gitops.merge_no_ff(root, str(self.state["branch"]), message)

    def rollback_merge(self, pre_merge_sha: str) -> None:
        gitops.git(self.cfg.root, "reset", "--hard", pre_merge_sha)
        self.log("merge_rolled_back", cycle=self.state.cycle, to=pre_merge_sha)

    def write_cost_report(self) -> None:
        reports = self.cfg.root / self.cfg.reports_rel
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"cost-{self.state.cycle:02d}.json"
        payload = {
            "cycle": self.state.cycle,
            "phase_id": self.state.get("phase_id"),
            "plan_sha": self.state.get("plan_sha"),
            "criteria_version": self.state.get("criteria_version"),
            "total_usd": self.state["cost"].get("total_usd"),
            "sessions": self.state["cost"].get("sessions", []),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.state["cost"]["sessions"] = []
        self.state["cost"]["total_usd"] = 0.0
        self.state.save()
        gitops.commit_all(self.cfg.root, f"cycle {self.state.cycle:02d}: cost report")

    # ---------------------------------------------------------------- prompts
    def build_prompt(self, role: str, *, increment=None, plan=None,
                     mode: str | None = None, attempt: int | None = None) -> str:
        return prompts.build(self, role, increment=increment, plan=plan,
                             mode=mode, attempt=attempt)

    # ------------------------------------------------------------------ run
    def run(self, until: str = "blocked", max_cycles: int = 0) -> str:
        cycles_before = self.state.cycle
        accepted_this_run = 0
        guard = 0
        while self.state.stage not in STOP_STAGES:
            guard += 1
            if guard > 200:
                self.block("transition guard tripped (200 steps) — inspect the log")
                break
            stage_before = self.state.stage
            outcome = self.step()
            if stage_before == "ACCEPT" and self.state.stage == "IDLE":
                accepted_this_run += 1
            if outcome == "stop" or self.state.stage in STOP_STAGES:
                break
            if until == "cycle" and accepted_this_run:
                break
            if until == "accept" and self.state.stage == "ACCEPT":
                break
            if max_cycles and accepted_this_run >= max_cycles:
                break
            if max_cycles and self.state.cycle - cycles_before > max_cycles:
                break
        return self.state.stage
