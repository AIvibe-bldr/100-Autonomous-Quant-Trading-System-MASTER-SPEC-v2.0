"""The runner's status file: its proof of life, written where an OUTSIDE process can read it.

A runner that has died cannot report that it has died (services/supervisor/runner.py says so), so the
runner rewrites this small JSON file on every tick and the watchdog (services/supervisor/watchdog.py, a
separate process) reads its age. Written atomically (temp file + rename) so a reader never sees half a file.
Also what the ops dashboard shows as the runner's state."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

FILE_NAME = "runner_status.json"

STARTING, WAITING, RUNNING, FAILED, STOPPED = "STARTING", "WAITING", "RUNNING", "FAILED", "STOPPED"


@dataclass(frozen=True)
class RunnerStatus:
    pid: int
    environment: str
    state: str                              # STARTING / WAITING / RUNNING (a session) / FAILED / STOPPED
    started_at: str
    updated_at: str
    session_started_at: Optional[str] = None
    last_session_date: Optional[str] = None
    last_session_at: Optional[str] = None
    sessions_run: int = 0
    failure: Optional[str] = None
    risk_state: str = "NORMAL"
    kill_switch_engaged: bool = False
    next_session_at: Optional[str] = None
    tick_seconds: float = 60.0


def write_status(path: Path | str, status: RunnerStatus) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(status)), encoding="utf-8")
    os.replace(tmp, path)


class StatusUnreadable(RuntimeError):
    pass


def read_status(path: Path | str) -> Optional[RunnerStatus]:
    """None if the runner has never written one. A file that exists but cannot be understood raises
    (it must not read as 'no runner')."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise StatusUnreadable(str(e)) from None
    try:
        return RunnerStatus(**json.loads(raw))
    except (ValueError, TypeError) as e:
        raise StatusUnreadable(f"runner status file is corrupt: {e}") from None


def age_seconds(status: RunnerStatus, now: datetime) -> float:
    return (now - datetime.fromisoformat(status.updated_at)).total_seconds()
