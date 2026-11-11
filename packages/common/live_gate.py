"""LIVE trading gate (MASTER SPEC §105-106; SAFETY_AUDIT "LIVE gate").

Nothing may send a LIVE entry order unless a named human has recorded an approval
that is still valid. The approval is a flag FILE (like the kill switch), so it can be
granted and revoked from another process without a write endpoint.

- **Fail closed.** No file, unreadable, malformed, expired, or an out-of-range stage:
  the gate is CLOSED. The default state of the world is "not approved".
- **Named human, bounded time.** Approving needs an operator, a reason, an explicit
  expiry (at most MAX_APPROVAL_DAYS ahead), and the largest exposure stage
  (fraction of the §104 capital ramp, in (0, 1]) the human accepts. An approval is never
  open-ended and never raises the ramp above what was stated.
- **Audited.** Every approve/revoke is appended to a log before the flag changes.
- **Scope.** It gates new ENTRIES only; protective stops and exits are never blocked by it.

The gate says "a human allowed LIVE for now". It does not replace the Risk Controller,
the Audit AI or the kill switch; it is one more independent condition."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from packages.common.clock import ensure_utc
from packages.common.flag_file import write_with_audit

DEFAULT_PATH = "var/live_gate.json"
ENV_VAR = "QUANT_LIVE_GATE_FILE"
MAX_APPROVAL_DAYS = 14


@dataclass(frozen=True)
class LiveGateStatus:
    is_open: bool
    detail: str
    operator: str = ""
    expires_at: str = ""
    max_exposure_stage: float = 0.0


def _closed(detail: str) -> LiveGateStatus:
    return LiveGateStatus(is_open=False, detail=detail)


class LiveGate:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> "LiveGate":
        return cls(os.environ.get(ENV_VAR) or DEFAULT_PATH)

    def status(self, now: datetime) -> LiveGateStatus:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _closed("no LIVE approval recorded")
        except OSError as e:
            return _closed(f"LIVE approval file unreadable ({e}) — failing closed")
        try:
            d = json.loads(raw)
            if not isinstance(d, dict) or d.get("approved") is not True:
                return _closed("LIVE approval revoked")
            expires = ensure_utc(datetime.fromisoformat(str(d["expires_at"])))
            stage = float(d["max_exposure_stage"])
            operator = str(d["operator"]).strip()
        except (ValueError, KeyError, TypeError):
            return _closed("LIVE approval file malformed — failing closed")
        if not operator or not (0 < stage <= 1.0) or stage != stage:
            return _closed("LIVE approval file invalid — failing closed")
        if ensure_utc(now) >= expires:
            return _closed(f"LIVE approval expired at {expires.isoformat()}")
        return LiveGateStatus(True, "approved", operator, expires.isoformat(), stage)

    def approve(self, operator: str, reason: str, at: datetime, expires_at: datetime,
                max_exposure_stage: float) -> None:
        operator, reason = (operator or "").strip(), (reason or "").strip()
        if not operator or not reason:
            raise ValueError("LIVE approval needs a named operator and a reason")
        if not (0 < max_exposure_stage <= 1.0):
            raise ValueError("max_exposure_stage must be in (0, 1]")
        at, expires_at = ensure_utc(at), ensure_utc(expires_at)
        if not (at < expires_at <= at + timedelta(days=MAX_APPROVAL_DAYS)):
            raise ValueError(f"expiry must be after now and within {MAX_APPROVAL_DAYS} days")
        write_with_audit(self.path, {
            "action": "approve", "approved": True, "operator": operator, "reason": reason,
            "at": at.isoformat(), "expires_at": expires_at.isoformat(),
            "max_exposure_stage": max_exposure_stage})

    def revoke(self, operator: str, reason: str, at: datetime) -> None:
        operator, reason = (operator or "").strip(), (reason or "").strip()
        if not operator or not reason:
            raise ValueError("LIVE revocation needs a named operator and a reason")
        write_with_audit(self.path, {"action": "revoke", "approved": False,
                                     "operator": operator, "reason": reason,
                                     "at": ensure_utc(at).isoformat()})
