"""Subcontractor adapter: render, spawn, collect.

Sessions are child processes, never subagents of whoever is watching the
dispatcher: closing the TUI must not kill the work. The adapter is the only
place that knows how a given CLI is invoked.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .gates import isolation_status

__all__ = ["SessionResult", "run_session", "PROMPT_DIR", "LOG_DIR"]

PROMPT_DIR = "prompts"
LOG_DIR = "logs"
CONTAINER_WORKDIR = "/work"
CONTAINER_PROMPT = "/hv-run/prompt.md"
CONTAINER_RUN = "/hv-run"


@dataclass
class SessionResult:
    role: str
    cycle: int
    attempt: int
    exit_code: int | None
    timed_out: bool
    duration_s: float
    log_path: Path
    prompt_path: Path
    usage: dict = field(default_factory=dict)
    isolation: str = "none"
    warning: str | None = None

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def _placeholder_values(cfg: Config, role: str, cycle: int, attempt: int,
                        prompt_path: Path, usage_path: Path, isolation: str,
                        workdir: Path | str = ".") -> dict:
    skill_root = Path(__file__).resolve().parents[2]
    if isolation == "docker":
        workdir_value = CONTAINER_WORKDIR
        prompt = CONTAINER_PROMPT
        usage = f"{CONTAINER_RUN}/{usage_path.name}"
        skill = "/hv"
    else:
        workdir_value = str(Path(workdir).resolve())
        prompt = str(prompt_path)
        usage = str(usage_path)
        skill = str(skill_root)
    return {
        "{prompt_file}": prompt,
        "{workdir}": workdir_value,
        "{role}": role,
        "{cycle}": f"{cycle:02d}",
        "{attempt}": str(attempt),
        "{usage_file}": usage,
        "{project}": cfg.name,
        "{root}": str(cfg.root),
        "{skill_root}": skill,
    }


def _render(cmd: str, values: dict) -> str:
    rendered = cmd
    for key, value in values.items():
        rendered = rendered.replace(key, shlex.quote(value))
    return rendered


def _usage_from_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    usage: dict = {}
    for key in ("estimated_cost_usd", "cost_usd", "total_cost_usd", "cost"):
        if key in data:
            try:
                usage["cost_usd"] = float(data[key])
            except (TypeError, ValueError):
                pass
            break
    for key in ("tokens_in", "input_tokens", "prompt_tokens"):
        if key in data:
            usage["tokens_in"] = data[key]
            break
    for key in ("tokens_out", "output_tokens", "completion_tokens"):
        if key in data:
            usage["tokens_out"] = data[key]
            break
    for key in ("api_calls", "model", "duration_s"):
        if key in data:
            usage[key] = data[key]
    return usage


def _usage_from_log(text: str) -> dict:
    for line in reversed(text.splitlines()):
        marker = line.find("HV_USAGE ")
        if marker == -1:
            continue
        try:
            data = json.loads(line[marker + len("HV_USAGE "):])
        except json.JSONDecodeError:
            continue
        usage = {}
        if "cost_usd" in data:
            usage["cost_usd"] = float(data["cost_usd"])
        for key in ("tokens_in", "tokens_out", "api_calls", "model"):
            if key in data:
                usage[key] = data[key]
        return usage
    return {}


def run_session(cfg: Config, role: str, prompt_text: str, workdir: Path,
                cycle: int, attempt: int, timeout: int | None = None,
                pins: dict | None = None) -> SessionResult:
    """Spawn one role session and collect its output, usage and log."""
    pins = pins or {}
    timeout = timeout or cfg.adapter_timeout
    cycle_dir = cfg.run_dir / PROMPT_DIR
    cycle_dir.mkdir(parents=True, exist_ok=True)
    log_dir = cfg.run_dir / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    cfg.cost_dir.mkdir(parents=True, exist_ok=True)

    stamp = f"{role}-c{cycle:02d}-a{attempt}"
    prompt_path = cycle_dir / f"{stamp}.md"
    log_path = log_dir / f"{stamp}.log"
    usage_path = cfg.cost_dir / f"{stamp}.usage.json"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    if usage_path.exists():
        usage_path.unlink()

    mode, warning = isolation_status(cfg)
    values = _placeholder_values(cfg, role, cycle, attempt, prompt_path, usage_path, mode, workdir)
    command = _render(cfg.adapter_cmd, values)

    env = {
        **os.environ,
        "HV_ROLE": role,
        "HV_PROJECT": cfg.name,
        "HV_CYCLE": str(cycle),
        "HV_ATTEMPT": str(attempt),
        "HV_PROMPT_FILE": str(prompt_path),
        "HV_ROOT": str(cfg.root),
        "HV_ISOLATION": mode,
        "HV_PLAN_SHA": str(pins.get("plan_sha") or ""),
        "HV_CRITERIA_VERSION": str(pins.get("criteria_version") or ""),
        "HV_INCREMENT": str(pins.get("increment") or ""),
        "HV_STAGE": str(pins.get("stage") or ""),
        "HV_USAGE_FILE": str(values["{usage_file}"]),
    }

    if mode == "docker":
        iso = cfg.isolation
        skill_root = Path(__file__).resolve().parents[2]
        argv = [
            "docker", "run", "--rm", "-i",
            "--network", str(iso.get("network", "none")),
            "--cpus", str(iso.get("cpus", 2)),
            "--memory", str(iso.get("memory", "4g")),
            "-v", f"{Path(workdir).resolve()}:{CONTAINER_WORKDIR}",
            "-v", f"{prompt_path}:{CONTAINER_PROMPT}:ro",
            "-v", f"{cfg.cost_dir}:{CONTAINER_RUN}",
            "-v", f"{skill_root}:/hv:ro",
            "-w", CONTAINER_WORKDIR,
            "-e", f"HV_ROLE={role}",
            "-e", f"HV_CYCLE={cycle}",
            "-e", f"HV_ATTEMPT={attempt}",
            "-e", f"HV_PROJECT={cfg.name}",
            "-e", f"HV_PLAN_SHA={pins.get('plan_sha') or ''}",
            "-e", f"HV_CRITERIA_VERSION={pins.get('criteria_version') or ''}",
            "-e", f"HV_INCREMENT={pins.get('increment') or ''}",
            "-e", f"HV_STAGE={pins.get('stage') or ''}",
        ]
        argv += [str(extra) for extra in iso.get("extra_args", []) or []]
        argv += [str(iso.get("image")), "sh", "-lc", command]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    else:
        argv = ["sh", "-lc", command]

    started = time.time()
    proc = subprocess.Popen(
        argv,
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
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
    log_path.write_text(output or "", encoding="utf-8")

    usage = _usage_from_file(usage_path) or _usage_from_log(output or "")
    return SessionResult(
        role=role,
        cycle=cycle,
        attempt=attempt,
        exit_code=code,
        timed_out=timed_out,
        duration_s=round(duration, 1),
        log_path=log_path,
        prompt_path=prompt_path,
        usage=usage,
        isolation=mode,
        warning=warning,
    )
