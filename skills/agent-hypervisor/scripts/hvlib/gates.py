"""Deterministic gates: build, tests, lint, setup, regression.

Gates are the only thing in the hypervisor that *proves* something. Everything
that a shell command can decide is decided here and never delegated to a
role's opinion.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config

__all__ = [
    "GateResult",
    "run_gate",
    "isolation_status",
    "run_shell",
    "parse_test_counts",
    "GATES",
]

GATES = ("setup", "build", "test", "lint")

TAIL_CHARS = 4000


@dataclass
class GateResult:
    name: str
    cmd: str
    status: str  # pass | fail | skip | timeout | missing
    exit_code: int | None = None
    duration_s: float = 0.0
    counts: str | None = None
    cold: bool = False
    isolation: str = "none"
    tail: str = ""

    def to_state(self) -> str | dict:
        if self.status == "pass" and self.counts:
            return self.counts
        return self.status

    def as_dict(self) -> dict:
        return asdict(self)


def isolation_status(cfg: Config) -> tuple[str, str | None]:
    """Return ``(effective_mode, warning)`` for the configured isolation."""
    mode = str(cfg.isolation.get("mode", "none")).lower()
    if mode == "none":
        return "none", None
    docker = shutil.which("docker")
    if not docker:
        return "none", "isolation.mode=docker but no `docker` binary on PATH"
    probe = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        hint = detail[-1] if detail else "docker daemon unreachable"
        return "none", f"isolation.mode=docker but the daemon is not reachable ({hint})"
    return "docker", None


def run_shell(cmd: str, workdir: Path, timeout: int, env: dict | None = None) -> tuple[int | None, str, float, bool]:
    """Run a shell command; return ``(exit, output, seconds, timed_out)``."""
    started = time.time()
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)
    proc = subprocess.Popen(
        ["sh", "-lc", cmd],
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=merged_env,
        start_new_session=True,
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
        code = None
    return code, output or "", time.time() - started, timed_out


def _docker_cmd(cfg: Config, cmd: str, workdir: Path) -> list[str]:
    iso = cfg.isolation
    args = [
        "docker", "run", "--rm", "-i",
        "--network", str(iso.get("network", "none")),
        "--cpus", str(iso.get("cpus", 2)),
        "--memory", str(iso.get("memory", "4g")),
        "-v", f"{Path(workdir).resolve()}:/work",
        "-w", "/work",
    ]
    args += [str(extra) for extra in iso.get("extra_args", []) or []]
    args += [str(iso.get("image")), "sh", "-lc", cmd]
    return args


def run_gate(cfg: Config, name: str, cmd: str, workdir: Path, *, cold: bool = False,
             timeout: int | None = None) -> GateResult:
    """Run one named gate in the configured isolation."""
    workdir = Path(workdir).resolve()
    timeout = timeout or cfg.gate_timeout
    if not cmd:
        return GateResult(name=name, cmd="", status="missing", cold=cold,
                          isolation="none", tail="no command configured")
    mode, warning = isolation_status(cfg)
    if cold and mode != "docker":
        return GateResult(
            name=name, cmd=cmd, status="skip", cold=True, isolation=mode,
            tail=warning or "cold-container check requires isolation.mode=docker",
        )
    started = time.time()
    if mode == "docker":
        proc = subprocess.Popen(
            _docker_cmd(cfg, cmd, workdir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            output, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = proc.communicate()
            code = None
        duration = time.time() - started
    else:
        code, output, duration, timed_out = run_shell(cmd, workdir, timeout)
    status = "timeout" if timed_out else ("pass" if code == 0 else "fail")
    return GateResult(
        name=name,
        cmd=cmd,
        status=status,
        exit_code=code,
        duration_s=round(duration, 1),
        counts=parse_test_counts(output) if name == "test" else None,
        cold=cold,
        isolation=mode,
        tail=(output or "")[-TAIL_CHARS:],
    )


_COUNT_PATTERNS = (
    re.compile(r"Ran (\d+) tests?.*?(?:OK|FAILED)", re.DOTALL),
    re.compile(r"=+ (\d+) passed(?:, (\d+) failed)?"),
    re.compile(r"(\d+)/(\d+) (?:tests? )?passed"),
)


def parse_test_counts(output: str) -> str | None:
    """Best-effort ``passed/total`` extraction for the cycle report."""
    for pattern in _COUNT_PATTERNS:
        match = pattern.search(output)
        if not match:
            continue
        groups = [group for group in match.groups() if group is not None]
        if len(groups) == 1:
            total = groups[0]
            failed = re.search(r"(\d+) failed", output)
            if failed:
                return f"{max(int(total) - int(failed.group(1)), 0)}/{total}"
            return f"{total}/{total}"
        if len(groups) >= 2 and groups[1] is not None and groups[1].isdigit():
            return f"{groups[0]}/{int(groups[0]) + int(groups[1])}"
        return f"{groups[0]}/{groups[0]}"
    return None
