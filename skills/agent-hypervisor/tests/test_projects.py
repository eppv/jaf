"""End-to-end tests: real git, real files, real gates, fake role sessions.

Every test drives the CLI as a user would (`hv init` → `hv run` → `hv status`),
so the assertions are about observable behaviour, not internal functions.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
HV = SKILL_ROOT / "scripts" / "hv.py"
FAKE = SKILL_ROOT / "examples" / "adapters" / "fake.py"

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
from hvlib import plan as plan_mod  # noqa: E402


class ProjectCase(unittest.TestCase):
    mode = "ok"
    limits: dict[str, int] = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hv-e2e-")
        self.project = Path(self._tmp.name) / "toy"
        self.project.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        (self.project / "README.md").write_text("# toy\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        adapter = f"FAKE_MODE={self.mode} {sys.executable} {FAKE} {{prompt_file}}"
        self.hv("init", "--name", "toy", "--adapter-cmd", adapter,
                "--isolation", "none", check=True)
        if self.limits:
            self.set_limits(**self.limits)

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def git(self, *args: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(["git", *args], cwd=str(cwd or self.project),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout

    def hv(self, *args: str, check: bool = False):
        proc = subprocess.run([sys.executable, str(HV), *args], cwd=str(self.project),
                              capture_output=True, text=True, timeout=900)
        if check and proc.returncode != 0:
            raise AssertionError(
                f"hv {' '.join(args)} failed ({proc.returncode})\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc

    def state(self) -> dict:
        return json.loads((self.project / "run" / "STATE.json").read_text(encoding="utf-8"))

    def log_events(self) -> list[dict]:
        path = self.project / "run" / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def set_limits(self, **values: int) -> None:
        path = self.project / "hv.toml"
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            if re.search(rf"(?m)^{key}\s*=", text):
                text = re.sub(rf"(?m)^{key}\s*=.*$", f"{key} = {value}", text)
            else:
                text = text.replace("[limits]\n", f"[limits]\n{key} = {value}\n", 1)
        path.write_text(text, encoding="utf-8")
        self.git("add", "hv.toml")
        self.git("commit", "-qm", f"limits: {values}")

    def set_adapter_timeout(self, seconds: int) -> None:
        path = self.project / "hv.toml"
        text = path.read_text(encoding="utf-8")
        head, _, tail = text.partition("[adapter]")
        self.assertTrue(tail, "hv.toml has no [adapter] section")
        if "[isolation]" in tail:
            body, _, rest = tail.partition("[isolation]")
        else:
            body, rest = tail, ""
        body = re.sub(r"(?m)^timeout\s*=.*$", f"timeout = {seconds}", body, count=1)
        path.write_text(head + "[adapter]" + body + ("[isolation]" + rest if rest else ""),
                        encoding="utf-8")
        self.git("add", "hv.toml")
        self.git("commit", "-qm", "adapter timeout")

    def spec(self) -> Path:
        return self.project / ".hv" / "spec"

    def worktree(self) -> Path:
        worktree = self.state().get("worktree")
        self.assertIsNotNone(worktree)
        return Path(worktree)

    def main_files(self) -> list[str]:
        return [line for line in self.git("ls-tree", "-r", "--name-only", "main").splitlines() if line]

    def reports(self) -> Path:
        return self.worktree() / "docs" / "reports"


class SpecSkeletonTest(ProjectCase):
    def test_spec_branch_has_exactly_the_contract(self):
        files = set(self.git("ls-tree", "-r", "--name-only", "spec").split())
        self.assertEqual(
            files,
            {
                "PLAN.md",
                "DECISIONS.md",
                "env/ENV.md",
                "roles/architect.md",
                "roles/dev.md",
                "roles/enabling.md",
                "roles/qa.md",
            },
            "the spec branch must hold the contract and nothing else",
        )


class HappyPathTest(ProjectCase):
    def test_full_project_reaches_done(self):
        self.hv("run", "--until", "done", check=True)
        state = self.state()
        self.assertEqual(state["stage"], "DONE")
        self.assertEqual(state["cycle"], 2)
        tags = self.git("tag", "--list").split()
        self.assertIn("cycle-01-accepted", tags)
        self.assertIn("cycle-02-accepted", tags)

        files = self.main_files()
        # the contract must not leak into code history
        self.assertNotIn("PLAN.md", files)
        self.assertFalse([name for name in files if name.startswith("env/")])
        self.assertNotIn("DECISIONS.md", files)
        self.assertIn("toy/calc.py", files)
        self.assertIn("docs/reports/QA-01-attempt-1.md", files)
        self.assertIn("docs/reports/cost-01.json", files)
        self.assertFalse([name for name in files if "__pycache__" in name])

        # main is green: the project's own gates pass on the merged result
        test_run = subprocess.run(["make", "test"], cwd=str(self.project),
                                  capture_output=True, text=True)
        self.assertEqual(test_run.returncode, 0, test_run.stdout + test_run.stderr)
        self.assertIn("OK", test_run.stdout + test_run.stderr)

    def test_second_cycle_skips_environment_stage(self):
        self.hv("run", "--until", "cycle", check=True)
        self.hv("run", "--until", "cycle", check=True)
        env_sessions = [event for event in self.log_events()
                        if event.get("event") == "session" and event.get("role") == "enabling"]
        self.assertEqual(len(env_sessions), 1, "environment must be built once, not per cycle")
        state = self.state()
        self.assertEqual(state["stage"], "IDLE")
        self.assertEqual(state["cycle"], 2)

    def test_status_restores_the_picture_after_a_fresh_session(self):
        self.hv("run", "--until", "cycle", check=True)
        # a new process is exactly what `/new` + `hv status` looks like
        proc = self.hv("status", "--json", check=True)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["stage"], "IDLE")
        self.assertEqual(payload["cycle"], 1)
        self.assertEqual(len(payload["plan_sha"]), 64)
        self.assertTrue(payload["gates"].get("regression"))
        text = self.hv("status", check=True).stdout
        self.assertIn("stage:", text)
        self.assertIn("next:", text)

    def test_plan_sha_survives_spec_side_edits(self):
        self.hv("run", "--until", "cycle", check=True)
        plan = plan_mod.load_plan(self.spec() / "PLAN.md")
        before = plan.get("1.2").plan_sha()

        roles = self.spec() / "roles" / "architect.md"
        roles.write_text(roles.read_text(encoding="utf-8") + "\nNote: extra guidance.\n",
                         encoding="utf-8")
        env_md = self.spec() / "env" / "ENV.md"
        env_md.write_text(env_md.read_text(encoding="utf-8") + "\n- extra: nothing\n",
                          encoding="utf-8")
        self.git("add", "-A", cwd=self.spec())
        self.git("commit", "-qm", "spec side edits", cwd=self.spec())

        after = plan_mod.load_plan(self.spec() / "PLAN.md").get("1.2").plan_sha()
        self.assertEqual(before, after, "roles/env edits must not move plan_sha")


class FailurePathTest(ProjectCase):
    mode = "broken_dev"
    limits = {"dev_rejections": 2, "total_rejections": 4}

    def test_gate_failure_rejects_then_fuses_to_human(self):
        self.hv("run", check=True)
        state = self.state()
        self.assertEqual(state["stage"], "AWAITING_HUMAN")
        self.assertGreaterEqual(state["counters"]["rejections"]["dev"], 2)
        self.assertTrue(state["awaiting"]["reason"])
        reject_notes = list(self.reports().glob("REJECT-*.md"))
        self.assertTrue(reject_notes, "a rejection must leave a written defect list")
        body = reject_notes[0].read_text(encoding="utf-8")
        self.assertIn("gate failed", body)
        self.assertIn("FAILED", body)
        # nothing was merged into main
        self.assertNotIn("cycle-01-accepted", self.git("tag", "--list"))


class BadReportTest(ProjectCase):
    mode = "bad_report"

    def test_missing_sections_retry_once_then_ask_a_human(self):
        self.hv("run", check=True)
        state = self.state()
        self.assertEqual(state["stage"], "AWAITING_HUMAN")
        self.assertIn("still invalid after a retry", state["awaiting"]["reason"])
        self.assertEqual(state["attempt"]["dev"], 2)
        retry_notes = list(self.reports().glob("RETRY-dev-*.md"))
        self.assertTrue(retry_notes)
        self.assertIn("missing required section", retry_notes[0].read_text(encoding="utf-8"))


class CrashTest(ProjectCase):
    mode = "crash"

    def test_two_crashes_block_the_cycle(self):
        proc = self.hv("run")
        self.assertEqual(proc.returncode, 2, "BLOCKED exits non-zero")
        state = self.state()
        self.assertEqual(state["stage"], "BLOCKED")
        self.assertIn("crashed", state["awaiting"]["reason"])
        self.assertEqual(state["counters"]["rejections"]["dev"], 0)
        self.assertEqual(state["counters"]["rejections"]["qa"], 0)


class TimeoutTest(ProjectCase):
    mode = "slow"

    def setUp(self):
        super().setUp()
        self.set_adapter_timeout(2)

    def test_timeout_does_not_consume_rejections(self):
        self.assertEqual(self.hv("run").returncode, 2)
        state = self.state()
        self.assertEqual(state["stage"], "BLOCKED")
        self.assertEqual(state["counters"]["rejections"]["dev"], 0)
        crashes = [event for event in self.log_events() if event.get("event") == "session_crash"]
        self.assertTrue(crashes)
        self.assertTrue(all(event.get("timed_out") for event in crashes))
        # the interrupted work is preserved as a checkpoint commit
        log = self.git("log", "--oneline", cwd=self.worktree())
        self.assertIn("wip/", log)


class ManifestTest(ProjectCase):
    mode = "manifest"
    limits = {"dev_rejections": 2, "total_rejections": 3}

    def test_undeclared_manifest_change_is_rejected(self):
        self.hv("run", check=True)
        notes = list(self.reports().glob("REJECT-*.md"))
        self.assertTrue(notes)
        body = "\n".join(path.read_text(encoding="utf-8") for path in notes)
        self.assertIn("manifest changed without an explicit deviation", body)
        self.assertIn("requirements.txt", body)


class QaFailureTest(ProjectCase):
    mode = "fail_qa"
    limits = {"qa_rejections": 2, "total_rejections": 4}

    def test_qa_failure_goes_to_dev_then_to_the_planner(self):
        self.hv("run", check=True)
        events = self.log_events()
        self.assertTrue(any(event.get("event") == "qa_failed" for event in events))
        self.assertTrue(any(event.get("event") == "reopened_by_plan" for event in events))
        state = self.state()
        self.assertGreaterEqual(state["reopen_count"], 1)
        self.assertEqual(state["stage"], "AWAITING_HUMAN")


class CriteriaDriftTest(ProjectCase):
    def test_criteria_bump_mid_cycle_reopens_it(self):
        self.hv("run", "--until", "accept", check=True)
        self.assertEqual(self.state()["stage"], "ACCEPT")

        path = self.spec() / "PLAN.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            '  - id: "1.1"\n    title: "calculator core"\n    depends_on: []\n    criteria_version: 1',
            '  - id: "1.1"\n    title: "calculator core"\n    depends_on: []\n    criteria_version: 2',
            1,
        )
        path.write_text(text, encoding="utf-8")
        self.git("add", "-A", cwd=self.spec())
        self.git("commit", "-qm", "criteria v2", cwd=self.spec())

        self.hv("run", check=True)
        state = self.state()
        self.assertGreaterEqual(state["reopen_count"], 1)
        self.assertTrue(any(event.get("event") == "reopened" for event in self.log_events()))


class QaPinTest(ProjectCase):
    def test_qa_artifact_with_foreign_plan_sha_blocks_acceptance(self):
        self.hv("run", "--until", "accept", check=True)
        qa_report = self.reports() / "QA-01-attempt-1.md"
        text = qa_report.read_text(encoding="utf-8")
        qa_report.write_text(re.sub(r'plan_sha: "[0-9a-f]+"', 'plan_sha: "deadbeef"', text),
                             encoding="utf-8")
        self.git("add", "-A", cwd=self.worktree())
        self.git("commit", "-qm", "tamper qa pins", cwd=self.worktree())

        self.hv("run", check=True)
        state = self.state()
        self.assertEqual(state["stage"], "AWAITING_HUMAN")
        self.assertIn("plan_sha mismatch", state["awaiting"]["reason"])


class HumanDecisionTest(ProjectCase):
    mode = "bad_report"

    def test_decision_is_required_before_state_changes(self):
        self.hv("run", check=True)
        self.assertEqual(self.state()["stage"], "AWAITING_HUMAN")

        without = self.hv("resolve", "--decision", "D-999")
        self.assertNotEqual(without.returncode, 0)
        self.assertIn("not found in DECISIONS.md", without.stdout + without.stderr)

        decided = self.hv("decide", "--kind", "note", "--text", "restart dev manually",
                          check=True)
        decision_id = re.search(r"D-\d+", decided.stdout).group(0)
        resolved = self.hv("resolve", "--decision", decision_id, "--stage", "IDLE", check=True)
        self.assertIn("IDLE", resolved.stdout)
        self.assertEqual(self.state()["stage"], "IDLE")
        decisions_text = (self.spec() / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn(f"## {decision_id}", decisions_text)


class RejectCycleTest(ProjectCase):
    mode = "crash"

    def test_rejected_cycle_is_tagged_and_the_branch_disappears(self):
        self.assertEqual(self.hv("run").returncode, 2)
        self.assertEqual(self.state()["stage"], "BLOCKED")
        decided = self.hv("decide", "--kind", "reject", "--text", "bad idea", check=True)
        decision_id = re.search(r"D-\d+", decided.stdout).group(0)
        self.hv("reject", "--decision", decision_id, "--reason", "scope too big", check=True)

        tags = self.git("tag", "--list").split()
        self.assertIn("cycle-01-abandoned", tags)
        branches = self.git("branch", "--list").split()
        self.assertNotIn("cycle/01", branches)
        state = self.state()
        self.assertEqual(state["stage"], "IDLE")
        self.assertIsNone(state["branch"])


class TransferTest(ProjectCase):
    def test_bundle_clone_rebuild_reproduces_the_picture(self):
        self.hv("run", "--until", "cycle", check=True)
        before = self.state()
        bundle = Path(self._tmp.name) / "project.bundle"
        self.git("bundle", "create", str(bundle), "--all")

        clone = Path(self._tmp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(bundle), str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@example.com"],
                       check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "tester"], check=True)

        proc = subprocess.run([sys.executable, str(HV), "rebuild"], cwd=str(clone),
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rebuilt = json.loads((clone / "run" / "STATE.json").read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["cycle"], before["cycle"])
        self.assertEqual(rebuilt["stage"], "IDLE")
        clone_plan = plan_mod.load_plan(clone / ".hv" / "spec" / "PLAN.md")
        self.assertEqual(rebuilt["plan_sha"], clone_plan.get("1.2").plan_sha())
        self.assertEqual(clone_plan.accepted_ids(), ["1.1"])

        # the gate reproduces on the new machine
        gate = subprocess.run([sys.executable, str(HV), "gate"], cwd=str(clone),
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertIn("test", gate.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
