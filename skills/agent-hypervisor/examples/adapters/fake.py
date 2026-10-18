#!/usr/bin/env python3
"""Deterministic stand-in for a real agent CLI.

The dispatcher does not care who renders a session prompt, which is why the
protocol can be exercised end-to-end without spending tokens. This adapter
plays every role for the toy calculator project in ``examples/toy`` and writes
the same artifacts a real agent would write.

``FAKE_MODE`` reproduces failures deterministically:

    ok           normal behaviour
    broken_dev   developer writes code that fails a criterion
    bad_report   developer report misses mandatory sections
    fail_qa      QA returns a fail verdict with a reproduction
    bad_qa       QA report misses a verdict for one criterion
    crash        session exits non-zero without writing an artifact
    manifest     developer touches a manifest and does not declare it
    slow         session sleeps (timeout path)

Run it as::

    FAKE_MODE=ok python3 {skill_root}/examples/adapters/fake.py {prompt_file}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROLE = os.environ.get("HV_ROLE", "")
CYCLE = int(os.environ.get("HV_CYCLE", "1") or 1)
ATTEMPT = int(os.environ.get("HV_ATTEMPT", "1") or 1)
INCREMENT = os.environ.get("HV_INCREMENT", "") or "1.1"
PLAN_SHA = os.environ.get("HV_PLAN_SHA", "")
CRITERIA_VERSION = os.environ.get("HV_CRITERIA_VERSION", "1") or "1"
STAGE = os.environ.get("HV_STAGE", "")
MODE = os.environ.get("FAKE_MODE", "ok")

WORKDIR = Path.cwd()
SPEC = WORKDIR / ".spec"
REPORTS = WORKDIR / "docs" / "reports"
SKILL_ROOT = Path(__file__).resolve().parents[2]
TOY = SKILL_ROOT / "examples" / "toy"
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from hvlib import miniyaml  # noqa: E402


# --------------------------------------------------------------------- utils
def say(message: str) -> None:
    print(f"[fake:{ROLE}] {message}", flush=True)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    say(f"wrote {path.relative_to(WORKDIR)}")


def toy_file(name: str) -> str:
    path = TOY / name
    if not path.exists():
        raise SystemExit(f"toy fixture missing: {path}")
    return path.read_text(encoding="utf-8")


def render(name: str, **values: str) -> str:
    """Fill ``{{key}}`` in a toy template without touching single-brace text."""
    text = toy_file(name)
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def usage_report() -> None:
    path = os.environ.get("HV_USAGE_FILE")
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"estimated_cost_usd": 0.002, "tokens_in": 1200,
                    "tokens_out": 400, "model": "fake", "api_calls": 1}),
        encoding="utf-8",
    )


def increment_entry(increment_id: str) -> dict | None:
    plan = SPEC / "PLAN.md"
    if not plan.exists():
        return None
    try:
        front, _ = miniyaml.parse_front_matter(plan.read_text(encoding="utf-8"))
    except miniyaml.MiniYAMLError:
        return None
    for raw in front.get("increments") or []:
        if isinstance(raw, dict) and str(raw.get("id")) == str(increment_id):
            return raw
    return None


def reports_dir() -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    return REPORTS


# ---------------------------------------------------------------- architect
def append_decision(kind: str, text: str) -> None:
    path = SPEC / "DECISIONS.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# DECISIONS\n\n"
    ids = [int(match) for match in re.findall(r"(?m)^## D-(\d{3,})", existing)]
    number = (max(ids) + 1) if ids else 1
    entry = (
        f"\n## D-{number:03d}\n"
        f"- ts: 2026-01-01T00:00:00+00:00\n"
        f"- actor: architect\n"
        f"- kind: {kind}\n"
        f"- cycle: {CYCLE}\n"
        f"- plan_sha: {PLAN_SHA[:12] or '-'}\n"
        f"- text: {text}\n"
    )
    path.write_text(existing.rstrip("\n") + "\n" + entry, encoding="utf-8")
    say(f"appended decision D-{number:03d}")


def architect_plan() -> None:
    write(SPEC / "PLAN.md", toy_file("plan.md"))
    append_decision("plan", "initial plan: increments 1.1 and 1.2 with executable criteria")


def architect_accept() -> None:
    plan_path = SPEC / "PLAN.md"
    text = plan_path.read_text(encoding="utf-8")
    marker = f'  - id: "{INCREMENT}"'
    if marker not in text:
        say(f"increment {INCREMENT} not found in PLAN.md")
        return
    head, tail = text.split(marker, 1)
    rest_of_id, remainder = tail.split("\n", 1)
    if "accepted_in:" not in remainder.split("  - id:", 1)[0]:
        lines = remainder.split("\n")
        lines.insert(0, f"    accepted_in: {CYCLE}")
        remainder = "\n".join(lines)
    text = head + marker + rest_of_id + "\n" + remainder
    text = re.sub(r"(?m)^plan_ver:\s*(\d+)",
                  lambda match: f"plan_ver: {int(match.group(1)) + 1}", text, count=1)
    write(plan_path, text)
    append_decision("accept", f"increment {INCREMENT} accepted in cycle {CYCLE}")


def architect_replan() -> None:
    """Two failed rounds: revise the criteria instead of asking for another patch."""
    plan_path = SPEC / "PLAN.md"
    text = plan_path.read_text(encoding="utf-8")
    entry = increment_entry(INCREMENT) or {}
    version = int(entry.get("criteria_version") or 1) + 1
    marker = f'  - id: "{INCREMENT}"'
    head, tail = text.split(marker, 1)
    own_block, rest = tail.split("criteria_version: ", 1)
    _, after = rest.split("\n", 1)
    text = head + marker + own_block + "criteria_version: " + str(version) + "\n" + after
    write(plan_path, text)
    append_decision(
        "criteria-change",
        f"{INCREMENT}: criteria bumped to v{version} after two failed rounds",
    )


def run_architect() -> None:
    if increment_entry(INCREMENT) is None:
        architect_plan()
    elif STAGE == "ACCEPT":
        architect_accept()
    else:
        architect_replan()


# ----------------------------------------------------------------- enabling
def run_enabling() -> None:
    write(SPEC / "env" / "ENV.md", toy_file("env.md"))
    write(WORKDIR / "Makefile", toy_file("Makefile"))
    write(WORKDIR / "tests" / ".gitkeep", "")


# ---------------------------------------------------------------------- dev
def run_dev() -> None:
    deviations = "none"
    write(WORKDIR / "toy" / "__init__.py", toy_file("src/toy/__init__.py"))
    if INCREMENT.startswith("1.2"):
        write(WORKDIR / "toy" / "calc.py", toy_file("src/toy/calc_full.py"))
        write(WORKDIR / "toy" / "__main__.py", toy_file("src/toy/main.py"))
        write(WORKDIR / "tests" / "test_core.py", toy_file("tests/test_core.py"))
        write(WORKDIR / "tests" / "test_power.py", toy_file("tests/test_power.py"))
    else:
        source = "src/toy/calc_broken.py" if MODE == "broken_dev" else "src/toy/calc_core.py"
        write(WORKDIR / "toy" / "calc.py", toy_file(source))
        write(WORKDIR / "tests" / "test_core.py", toy_file("tests/test_core.py"))

    if MODE == "manifest":
        write(WORKDIR / "requirements.txt", toy_file("requirements.txt"))

    template = "reports/dev_bad.md" if MODE == "bad_report" else "reports/dev.md"
    body = render(
        template,
        plan_sha=PLAN_SHA,
        criteria_version=CRITERIA_VERSION,
        cycle=str(CYCLE),
        attempt=str(ATTEMPT),
        increment=INCREMENT,
        deviations=deviations,
    )
    write(reports_dir() / f"STAGE-{CYCLE:02d}-attempt-{ATTEMPT}.md", body)


# ---------------------------------------------------------------------- qa
def run_qa() -> None:
    entry = increment_entry(INCREMENT) or {}
    criteria = [item for item in (entry.get("acceptance") or []) if isinstance(item, dict)]
    rows: list[str] = []
    defects: list[str] = []
    for index, criterion in enumerate(criteria):
        cid = str(criterion.get("id"))
        check = str(criterion.get("check") or "true")
        if MODE == "bad_qa" and index == 0:
            continue
        proc = subprocess.run(["sh", "-lc", check], cwd=str(WORKDIR),
                              capture_output=True, text=True, timeout=120)
        passed = proc.returncode == 0
        if MODE == "fail_qa" and index == 0:
            passed = False
        rows.append(f"| {cid} | {'pass' if passed else 'fail'} | "
                    f"`{check}` -> exit {proc.returncode} |")
        if not passed:
            observed = (proc.stdout + proc.stderr).strip()[:200] or "no output"
            defects.append(
                f"- {cid}: `{check}` exited {proc.returncode}\n"
                f"  - observed: {observed}\n"
                f"  - expected: exit 0"
            )
    body = render(
        "reports/qa.md",
        plan_sha=PLAN_SHA,
        criteria_version=CRITERIA_VERSION,
        cycle=str(CYCLE),
        attempt=str(ATTEMPT),
        rows="\n".join(rows) or "| C-x | fail | no criteria |",
        defects="\n".join(defects) or "none",
    )
    write(reports_dir() / f"QA-{CYCLE:02d}-attempt-{ATTEMPT}.md", body)


# -------------------------------------------------------------------- main
HANDLERS = {
    "architect": run_architect,
    "enabling": run_enabling,
    "dev": run_dev,
    "qa": run_qa,
}


def main() -> int:
    prompt_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HV_PROMPT_FILE", "")
    size = Path(prompt_path).stat().st_size if prompt_path and Path(prompt_path).exists() else 0
    say(f"mode={MODE} cycle={CYCLE} attempt={ATTEMPT} increment={INCREMENT} "
        f"stage={STAGE} prompt={size}b")
    if MODE == "crash":
        say("crashing on purpose")
        return 3
    if MODE == "slow":
        time.sleep(60)
        return 0
    handler = HANDLERS.get(ROLE)
    if handler is None:
        say(f"unknown role {ROLE!r}")
        return 2
    handler()
    usage_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
