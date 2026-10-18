"""Unit tests for the hv building blocks (no project, no sessions)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from hvlib import artifacts, decisions, gates, miniyaml  # noqa: E402
from hvlib import plan as plan_mod  # noqa: E402
from hvlib.config import load_config  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PLAN_TEXT = (FIXTURES / "plan.md").read_text(encoding="utf-8")


class MiniYamlTest(unittest.TestCase):
    def test_nested_plan_front_matter(self):
        front, body = miniyaml.parse_front_matter(PLAN_TEXT)
        self.assertEqual(front["plan_ver"], 3)
        increments = front["increments"]
        self.assertEqual(len(increments), 2)
        first = increments[0]
        self.assertEqual(first["id"], "1.1")
        self.assertEqual(first["depends_on"], [])
        self.assertEqual([c["id"] for c in first["acceptance"]], ["C-1.1.1", "C-1.1.2"])
        # escaped quotes survive
        self.assertIn('assert c.add(2,3)==5', first["acceptance"][0]["check"])
        self.assertTrue(body.lstrip().startswith("# Plan"))

    def test_inline_list_of_strings(self):
        parsed = miniyaml.parse('depends_on: ["1.1", "1.2"]\nempty: []\n')
        self.assertEqual(parsed["depends_on"], ["1.1", "1.2"])
        self.assertEqual(parsed["empty"], [])

    def test_comments_and_blank_lines_are_ignored(self):
        parsed = miniyaml.parse("# leading comment\nkey: value  # trailing\n\nother: 2\n")
        self.assertEqual(parsed, {"key": "value", "other": 2})

    def test_unsupported_flow_mapping_raises(self):
        with self.assertRaises(miniyaml.MiniYAMLError):
            miniyaml.parse("value: {a: b}\n")

    def test_indentation_error_raises(self):
        with self.assertRaises(miniyaml.MiniYAMLError):
            miniyaml.parse("a: 1\n  b: 2\n")


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = plan_mod.parse_plan_text(PLAN_TEXT, Path("PLAN.md"))

    def test_plan_sha_is_stable_across_cosmetic_edits(self):
        increment = self.plan.get("1.1")
        sha_before = increment.plan_sha()
        # trailing whitespace and a collapsed blank line do not change the hash
        edited = PLAN_TEXT.replace("Body of the first increment.", "Body of the first increment.   ")
        edited = edited.replace("\n\nBody of the second", "\n\n\nBody of the second")
        again = plan_mod.parse_plan_text(edited).get("1.1")
        self.assertEqual(sha_before, again.plan_sha())

    def test_plan_sha_changes_with_the_block(self):
        increment = self.plan.get("1.1")
        sha_before = increment.plan_sha()
        edited = PLAN_TEXT.replace("Body of the first increment.", "Body of the first increment, revised.")
        self.assertNotEqual(sha_before, plan_mod.parse_plan_text(edited).get("1.1").plan_sha())

    def test_plan_sha_ignores_neighbouring_increments(self):
        increment = self.plan.get("1.1")
        sha_before = increment.plan_sha()
        edited = PLAN_TEXT.replace("power works", "power works, reworded")
        self.assertEqual(sha_before, plan_mod.parse_plan_text(edited).get("1.1").plan_sha())

    def test_next_increment_respects_dependencies(self):
        self.assertEqual(self.plan.next_increment().id, "1.1")
        increment = self.plan.get("1.1")
        accepted = PLAN_TEXT.replace(
            '  - id: "1.1"\n    title: "first"',
            '  - id: "1.1"\n    accepted_in: 1\n    title: "first"',
        )
        plan = plan_mod.parse_plan_text(accepted)
        self.assertEqual(plan.next_increment().id, "1.2")

    def test_validation_flags_missing_history_and_checks(self):
        bad = PLAN_TEXT.replace(
            '      - id: "C-1.1.1"\n        version: 1',
            '      - id: "C-1.1.1"\n        version: 2',
        )
        problems = plan_mod.validate_plan(plan_mod.parse_plan_text(bad))
        self.assertTrue(any("acceptance_history" in problem for problem in problems))

    def test_validation_flags_duplicate_ids(self):
        bad = PLAN_TEXT.replace('  - id: "1.2"', '  - id: "1.1"')
        with self.assertRaises(plan_mod.PlanError):
            plan_mod.parse_plan_text(bad)


class ArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.plan = plan_mod.parse_plan_text(PLAN_TEXT)
        self.increment = self.plan.get("1.1")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, body: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def report(self, name: str, extra: str = "") -> artifacts.Report:
        return artifacts.parse_report(self.write(name, extra), artifacts.STAGE_REPORT)

    def test_missing_section_is_a_violation(self):
        report = self.report(
            "STAGE.md",
            "---\nplan_sha: \"%s\"\ncriteria_version: 1\nrole: dev\ncycle: 1\nattempt: 1\n---\n"
            "## What was done\n\nstuff\n" % self.increment.plan_sha(),
        )
        problems = artifacts.validate_report(
            report, self.increment, self.increment.plan_sha(), 1
        )
        self.assertTrue(any("how to verify" in problem for problem in problems))
        self.assertTrue(any("deviations" in problem for problem in problems))

    def test_plan_sha_mismatch_is_a_violation(self):
        text = (
            "---\nplan_sha: \"deadbeef\"\ncriteria_version: 1\nrole: dev\ncycle: 1\nattempt: 1\n---\n"
            "## What was done\nx\n## Deviations and implicit decisions\nnone\n"
            "## Open questions\nnone\n## How to verify\nmake test\n"
        )
        report = self.report("STAGE2.md", text)
        problems = artifacts.validate_report(report, self.increment, "abc123", 1)
        self.assertTrue(any("plan_sha mismatch" in problem for problem in problems))

    def test_criteria_version_mismatch_is_a_violation(self):
        text = (
            "---\nplan_sha: \"%s\"\ncriteria_version: 7\nrole: dev\ncycle: 1\nattempt: 1\n---\n"
            "## What was done\nx\n## Deviations and implicit decisions\nnone\n"
            "## Open questions\nnone\n## How to verify\nmake test\n"
        ) % self.increment.plan_sha()
        report = self.report("STAGE3.md", text)
        problems = artifacts.validate_report(
            report, self.increment, self.increment.plan_sha(), 1
        )
        self.assertTrue(any("criteria_version mismatch" in problem for problem in problems))

    def test_qa_missing_verdict_is_a_violation(self):
        text = (
            "---\nplan_sha: \"%s\"\ncriteria_version: 1\nrole: qa\ncycle: 1\nattempt: 1\n---\n"
            "## Verdicts\n| criterion | verdict |\n| C-1.1.1 | pass |\n"
            "## Defects\nnone\n## Debts\nnone\n"
        ) % self.increment.plan_sha()
        path = self.write("QA.md", text)
        report = artifacts.parse_report(path, artifacts.QA_REPORT)
        problems = artifacts.validate_report(
            report, self.increment, self.increment.plan_sha(), 1
        )
        self.assertTrue(any("C-1.1.2" in problem for problem in problems))

    def test_verdicts_are_parsed_from_the_table(self):
        body = (
            "## Verdicts\n\n| criterion | verdict | evidence |\n|---|---|---|\n"
            "| C-1.1.1 | pass | ok |\n| C-1.1.2 | fail | broke |\n"
        )
        verdicts = artifacts.criteria_verdicts(body)
        self.assertEqual(verdicts, {"C-1.1.1": "pass", "C-1.1.2": "fail"})

    def test_prompt_rendering_keeps_shell_braces(self):
        rendered = artifacts.render_prompt("a {{x}} b $(cat {file}) c", {"x": "1"})
        self.assertEqual(rendered, "a 1 b $(cat {file}) c")


class DecisionsTest(unittest.TestCase):
    def test_append_only_ids_increment(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            first = decisions.append_decision(worktree, kind="note", text="one", actor="human")
            second = decisions.append_decision(worktree, kind="criteria-change",
                                               text="two", actor="architect", cycle=2)
            self.assertEqual((first.id, second.id), ("D-001", "D-002"))
            loaded = decisions.load_decisions(worktree)
            self.assertEqual([item.id for item in loaded], ["D-001", "D-002"])
            self.assertEqual(loaded[1].kind, "criteria-change")
            self.assertEqual(loaded[1].cycle, 2)


class GatesTest(unittest.TestCase):
    def make_config(self, root: Path, isolation: str = "none") -> Config:
        (root / "hv.toml").write_text(
            '[project]\nname = "t"\n\n[adapter]\ncmd = "true {prompt_file}"\n\n'
            f'[isolation]\nmode = "{isolation}"\n',
            encoding="utf-8",
        )
        return load_config(root)

    def test_missing_gate_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_config(Path(tmp))
            result = gates.run_gate(cfg, "build", "", Path(tmp))
            self.assertEqual(result.status, "missing")

    def test_gate_records_failure_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_config(Path(tmp))
            result = gates.run_gate(cfg, "test", "echo boom && exit 3", Path(tmp))
            self.assertEqual(result.status, "fail")
            self.assertIn("boom", result.tail)

    def test_cold_gate_requires_container_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.make_config(Path(tmp))
            result = gates.run_gate(cfg, "setup", "true", Path(tmp), cold=True)
            self.assertEqual(result.status, "skip")
            self.assertTrue(result.cold)

    def test_test_counts_parsing(self):
        self.assertEqual(gates.parse_test_counts("Ran 4 tests in 0.01s\n\nOK"), "4/4")
        self.assertEqual(gates.parse_test_counts("Ran 5 tests\nFAILED"), "5/5")
        self.assertEqual(gates.parse_test_counts("== 3 passed, 1 failed =="), "3/4")

    def test_container_gate_mounts_only_the_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "worktrees" / "cycle-01"
            workdir.mkdir(parents=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            log = root / "docker.log"
            stub = bin_dir / "docker"
            shutil.copyfile(FIXTURES / "docker_stub.py", stub)
            stub.chmod(0o755)
            cfg = self.make_config(root, isolation="docker")
            env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "DOCKER_ARGS_LOG": str(log)}
            old_env = dict(os.environ)
            os.environ.update(env)
            try:
                result = gates.run_gate(cfg, "test", "true", workdir)
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            self.assertEqual(result.status, "pass")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            run_call = next(call for call in calls if call and call[0] == "run")
            joined = " ".join(run_call)
            self.assertIn("--network none", joined)
            mounts = [run_call[index + 1] for index, item in enumerate(run_call) if item == "-v"]
            self.assertEqual(mounts, [f"{workdir.resolve()}:/work"],
                             "only the cycle worktree may be mounted")
            self.assertNotIn(".ssh", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
