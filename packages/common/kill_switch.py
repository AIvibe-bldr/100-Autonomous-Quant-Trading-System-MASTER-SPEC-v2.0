"""Operator kill switch (SAFETY_AUDIT F5; instruction §12).

A flag FILE, so a CLI in another OS process can reach a running system without
a write endpoint or IPC server. Properties that matter for a control like this:

- **Fail closed.** A missing file means "not engaged" (the default state), but a
  file that exists and cannot be understood is treated as ENGAGED: a corrupt
  flag must never silently read as "all clear".
- **Named human.** Every engage/clear needs a non-empty operator and reason, and
  is appended to a separate audit log (who / when / why), never rewritten.
- **No automatic resume.** Nothing in the system clears the flag, and a process
  that has already halted stays halted when the flag is cleared; resuming means
  an explicit `clear` AND a restart of the runner.
- Atomic writes (temp file + rename), so a reader never sees a half-written flag.

Scope: it halts NEW ENTRIES only. Protective stops and risk-reducing exits stay
allowed (MASTER SPEC §43)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_PATH = "var/kill_switch.json"
ENV_VAR = "QUANT_KILL_SWITCH_FILE"


@dataclass(frozen=True)
class KillSwitchStatus:
    engaged: bool
    operator: str = ""
    reason: str = ""
    at: str = ""


class KillSwitch:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> "KillSwitch":
        return cls(os.environ.get(ENV_VAR) or DEFAULT_PATH)

    @property
    def log_path(self) -> Path:
        return self.path.with_name(self.path.name + ".log")

    def status(self) -> KillSwitchStatus:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return KillSwitchStatus(engaged=False)
        except OSError as e:
            return KillSwitchStatus(engaged=True, operator="system",
                                    reason=f"kill switch file unreadable ({e}) — failing closed")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("engaged"), bool):
                raise ValueError("malformed")
            return KillSwitchStatus(engaged=data["engaged"], operator=str(data.get("operator", "")),
                                    reason=str(data.get("reason", "")), at=str(data.get("at", "")))
        except ValueError:
            return KillSwitchStatus(engaged=True, operator="system",
                                    reason="kill switch file unreadable (corrupt) — failing closed")

    def engage(self, operator: str, reason: str, at: datetime) -> None:
        self._change("engage", True, operator, reason, at)

    def clear(self, operator: str, reason: str, at: datetime) -> None:
        self._change("clear", False, operator, reason, at)

    def _change(self, action: str, engaged: bool, operator: str, reason: str,
                at: datetime) -> None:
        operator, reason = (operator or "").strip(), (reason or "").strip()
        if not operator or not reason:
            raise ValueError("kill switch changes need a named operator and a reason")
        entry = {"action": action, "engaged": engaged, "operator": operator,
                 "reason": reason, "at": at.isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as log:           # audit first: never lose the record
            log.write(json.dumps(entry) + "\n")
            log.flush()
            os.fsync(log.fileno())
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(entry))
        os.replace(tmp, self.path)


def describe(status: Optional[KillSwitchStatus]) -> str:
    if status is None or not status.engaged:
        return "not engaged"
    return f"ENGAGED by {status.operator or '?'}: {status.reason}"
