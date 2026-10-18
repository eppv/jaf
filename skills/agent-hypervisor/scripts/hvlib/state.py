"""STATE.json and the append-only dispatcher log.

The dispatcher is the only writer of both files. ``STATE.json`` is an index:
it can always be rebuilt from git artifacts (see ``hv rebuild``), so a stale
or lost STATE is never data loss.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["State", "StateError", "new_state", "load_state", "save_state", "log_event", "now"]

SCHEMA = 1

STAGES = (
    "IDLE",
    "PLANNING",
    "ENV",
    "DEV",
    "QA",
    "ACCEPT",
    "AWAITING_HUMAN",
    "BLOCKED",
    "DONE",
)


class StateError(RuntimeError):
    """STATE.json is unreadable or internally inconsistent."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_state(project: str) -> dict:
    return {
        "schema": SCHEMA,
        "project": project,
        "cycle": 0,
        "phase_id": None,
        "stage": "IDLE",
        "attempt": {"env": 0, "dev": 0, "architect": 0, "qa": 0},
        "counters": {
            "rejections": {"dev": 0, "qa": 0},
            "crashes": {"env": 0, "dev": 0, "qa": 0, "architect": 0},
            "env_attempts": 0,
            "total_rejections": 0,
            "artifact_retries": {"env": 0, "dev": 0, "qa": 0, "architect": 0},
        },
        "plan_sha": None,
        "plan_ver": 1,
        "criteria_version": 1,
        "criteria_snapshot": {},
        "env_sha": None,
        "branch": None,
        "worktree": None,
        "gates": {},
        "gate_details": {},
        "isolation": "none",
        "awaiting": None,
        "reopened": False,
        "reopen_count": 0,
        "cost": {"total_usd": 0.0, "sessions": []},
        "rebuild_notes": [],
        "history": [],
        "updated": now(),
    }


class State:
    def __init__(self, data: dict, path: Path):
        self.data = data
        self.path = path

    # -- attribute access -------------------------------------------------
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def stage(self) -> str:
        return self.data["stage"]

    @stage.setter
    def stage(self, value: str) -> None:
        if value not in STAGES:
            raise StateError(f"unknown stage {value!r}")
        self.data["stage"] = value

    @property
    def cycle(self) -> int:
        return int(self.data.get("cycle") or 0)

    @property
    def counters(self) -> dict:
        return self.data["counters"]

    def counter(self, group: str, key: str) -> int:
        return int(self.data["counters"].get(group, {}).get(key, 0))

    def bump(self, group: str, key: str, amount: int = 1) -> int:
        bucket = self.data["counters"].setdefault(group, {})
        bucket[key] = int(bucket.get(key, 0)) + amount
        return bucket[key]

    def bump_total(self, amount: int = 1) -> int:
        total = int(self.data["counters"].get("total_rejections") or 0) + amount
        self.data["counters"]["total_rejections"] = total
        return total

    def reset_counters(self, reset_total: bool = True) -> None:
        total = 0 if reset_total else int(self.data["counters"].get("total_rejections") or 0)
        self.data["counters"] = {
            "rejections": {"dev": 0, "qa": 0},
            "crashes": {"env": 0, "dev": 0, "qa": 0, "architect": 0},
            "env_attempts": 0,
            "total_rejections": total,
            "artifact_retries": {"env": 0, "dev": 0, "qa": 0, "architect": 0},
        }

    # -- persistence ------------------------------------------------------
    def save(self) -> None:
        self.data["updated"] = now()
        save_state(self.path, self.data)


def load_state(path: Path) -> State:
    path = Path(path)
    if not path.exists():
        raise StateError(
            f"{path} not found — run `hv init` or `hv rebuild` first"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"{path}: invalid JSON ({exc})") from exc
    if data.get("schema") != SCHEMA:
        raise StateError(f"{path}: unsupported schema {data.get('schema')!r}")
    if data.get("stage") not in STAGES:
        raise StateError(f"{path}: unknown stage {data.get('stage')!r}")
    return State(data, path)


def save_state(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".STATE.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def log_event(path: Path, event: str, **fields) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": now(), "event": event}
    record.update({key: value for key, value in fields.items() if value is not None})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
