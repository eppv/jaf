"""Project configuration (``hv.toml``) for the hypervisor dispatcher."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Config", "ConfigError", "load_config", "CONFIG_NAME"]

CONFIG_NAME = "hv.toml"

DEFAULTS = {
    "project": {
        "name": "project",
        "main": "main",
        "spec_branch": "spec",
    },
    "dirs": {
        "worktrees": "worktrees",
        "run": "run",
        "spec": ".spec",
        "hv": ".hv",
        "reports": "docs/reports",
    },
    "gates": {
        "setup": "",
        "build": "",
        "test": "",
        "lint": "",
        "timeout": 1800,
        "manifest_patterns": [
            "pyproject.toml",
            "requirements*.txt",
            "uv.lock",
            "poetry.lock",
            "Pipfile.lock",
            "package-lock.json",
            "package.json",
            "go.mod",
            "go.sum",
            "Cargo.toml",
            "Cargo.lock",
        ],
    },
    "adapter": {
        "cmd": "",
        "timeout": 3600,
    },
    "limits": {
        "dev_rejections": 2,
        "qa_rejections": 2,
        "env_attempts": 2,
        "total_rejections": 6,
        "crashes": 1,
    },
    "isolation": {
        "mode": "docker",
        "image": "python:3.12-slim",
        "network": "none",
        "cpus": 2,
        "memory": "4g",
        "extra_args": [],
    },
}

# Role -> stage that runs it, used by prompts and permissions.
ROLES = ("architect", "enabling", "dev", "qa")

# The enabling-engineer's attempt counter lives under `env` in STATE.
ATTEMPT_KEY = {"architect": "architect", "enabling": "env", "dev": "dev", "qa": "qa"}


def attempt_key(role: str) -> str:
    return ATTEMPT_KEY.get(role, role)


class ConfigError(ValueError):
    """hv.toml is missing, unreadable or incomplete."""


@dataclass
class Config:
    root: Path
    data: dict = field(default_factory=dict)

    # -- generic access ---------------------------------------------------
    def get(self, section: str, key: str, default=None):
        return self.data.get(section, {}).get(key, default)

    def section(self, name: str) -> dict:
        return self.data.get(name, {})

    # -- paths ------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.get("project", "name", "project")

    @property
    def main_branch(self) -> str:
        return self.get("project", "main", "main")

    @property
    def spec_branch(self) -> str:
        return self.get("project", "spec_branch", "spec")

    def path(self, key: str) -> Path:
        return self.root / self.get("dirs", key, DEFAULTS["dirs"][key])

    @property
    def run_dir(self) -> Path:
        return self.path("run")

    @property
    def state_path(self) -> Path:
        return self.run_dir / "STATE.json"

    @property
    def log_path(self) -> Path:
        return self.run_dir / "log.jsonl"

    @property
    def cost_dir(self) -> Path:
        return self.run_dir / "cost"

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def worktrees_dir(self) -> Path:
        return self.path("worktrees")

    @property
    def hv_dir(self) -> Path:
        return self.path("hv")

    @property
    def main_worktree(self) -> Path:
        return self.hv_dir / "main"

    @property
    def spec_worktree(self) -> Path:
        return self.hv_dir / "spec"

    @property
    def reports_rel(self) -> str:
        return self.get("dirs", "reports", "docs/reports")

    # -- gates ------------------------------------------------------------
    def gate_cmd(self, name: str) -> str:
        return self.get("gates", name, "") or ""

    @property
    def gate_timeout(self) -> int:
        return int(self.get("gates", "timeout", 1800))

    @property
    def manifest_patterns(self) -> list[str]:
        return list(self.get("gates", "manifest_patterns", []))

    # -- adapter ----------------------------------------------------------
    @property
    def adapter_cmd(self) -> str:
        return self.get("adapter", "cmd", "") or ""

    @property
    def adapter_timeout(self) -> int:
        return int(self.get("adapter", "timeout", 3600))

    # -- limits -----------------------------------------------------------
    @property
    def limits(self) -> dict:
        merged = dict(DEFAULTS["limits"])
        merged.update(self.section("limits"))
        return merged

    # -- isolation --------------------------------------------------------
    @property
    def isolation(self) -> dict:
        merged = dict(DEFAULTS["isolation"])
        merged.update(self.section("isolation"))
        return merged


def _merge_defaults(loaded: dict) -> dict:
    merged: dict = {}
    for section, defaults in DEFAULTS.items():
        values = dict(defaults)
        values.update(loaded.get(section, {}) or {})
        merged[section] = values
    for section, values in loaded.items():
        if section not in merged:
            merged[section] = values
    return merged


def load_config(root: Path | str) -> Config:
    root = Path(root).resolve()
    path = root / CONFIG_NAME
    if not path.exists():
        raise ConfigError(
            f"{path} not found — run `hv init` in the project root first"
        )
    try:
        loaded = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    data = _merge_defaults(loaded)

    cmd = data["adapter"]["cmd"]
    if not cmd:
        raise ConfigError(
            "adapter.cmd is empty in hv.toml — the dispatcher has no default "
            "executor; set e.g. cmd = 'hermes -z \"$(cat {prompt_file})\" --usage-file {usage_file}'"
        )
    if "{prompt_file}" not in cmd:
        raise ConfigError("adapter.cmd must reference {prompt_file}")

    mode = str(data["isolation"].get("mode", "none")).lower()
    if mode not in ("docker", "none"):
        raise ConfigError(f"isolation.mode must be 'docker' or 'none', got {mode!r}")

    return Config(root=root, data=data)
