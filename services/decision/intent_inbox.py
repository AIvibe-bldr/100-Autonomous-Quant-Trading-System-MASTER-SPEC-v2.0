"""Human intent inbox (MASTER SPEC §76-79): where "I want to buy X" is received, shown an analysis,
confirmed by the human, and only then offered to the trading pipeline.

A human's wish is never an order. The life of an intent is
    SUBMITTED -> ANALYZED -> CONFIRMED -> CONSUMED
with CANCELLED / REJECTED / EXPIRED as the other ends, and each step is an append-only event with a
NAMED operator. The pipeline consumes a CONFIRMED intent as a candidate that goes through the same
loss-control -> sizing -> allocation -> Audit -> Master Risk Controller -> Execution path as an AI
candidate (INV-8); this module has no handle on any of those.

Trust model (same as the kill switch and the LIVE gate): whoever can write the file on the host can
speak as an operator. Fail closed: if the log cannot be read, the pipeline acts on NO intent.

Only WANT_BUY is supported. A WANT_SELL would have to cancel the protective stop that holds the
shares in a cash account first; that is not done silently, so it is refused up front."""
from __future__ import annotations

import enum
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from services.decision.human_intent import IntentAction

DEFAULT_PATH = "var/human_intents.jsonl"
ENV_VAR = "QUANT_INTENT_LOG"
MAX_OPEN_INTENTS = 10
CONFIRMED_VALID_FOR = timedelta(days=2)       # a confirmation is about a moment, not a standing order
UNCONFIRMED_VALID_FOR = timedelta(days=7)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class IntentError(ValueError):
    """The request is refused (bad input, wrong state, or unsupported)."""


class IntentInboxUnreadable(RuntimeError):
    """The log exists but cannot be read; nothing may be acted on."""


class IntentStatus(str, enum.Enum):
    SUBMITTED = "SUBMITTED"
    ANALYZED = "ANALYZED"
    CONFIRMED = "CONFIRMED"
    CONSUMED = "CONSUMED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


_OPEN = (IntentStatus.SUBMITTED, IntentStatus.ANALYZED, IntentStatus.CONFIRMED)


@dataclass
class IntentRecord:
    intent_id: str
    symbol: str
    action: str
    stated_reason: str
    submitted_by: str
    submitted_at: datetime
    status: IntentStatus = IntentStatus.SUBMITTED
    analysis: Optional[dict[str, Any]] = None
    analyzed_at: Optional[datetime] = None
    confirmed_by: str = ""
    confirmed_at: Optional[datetime] = None
    outcome: str = ""                      # how it ended: order placed / why not traded / who cancelled
    history: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN


def _clean(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise IntentError(f"{what} is required")
    return value


class IntentInbox:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> "IntentInbox":
        return cls(os.environ.get(ENV_VAR) or DEFAULT_PATH)

    # -- storage -----------------------------------------------------------------
    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(event, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def records(self) -> list[IntentRecord]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        except OSError as e:
            raise IntentInboxUnreadable(str(e)) from None
        recs: dict[str, IntentRecord] = {}
        try:
            for line in filter(str.strip, lines):
                e = json.loads(line)
                kind, iid = e["event"], e["intent_id"]
                at = datetime.fromisoformat(e["at"])
                if kind == "submitted":
                    recs[iid] = IntentRecord(
                        intent_id=iid, symbol=e["symbol"], action=e["action"],
                        stated_reason=e["reason"], submitted_by=e["operator"], submitted_at=at,
                        history=[f"{at.isoformat()} submitted by {e['operator']}"])
                    continue
                r = recs[iid]
                r.history.append(f"{at.isoformat()} {kind}" + (f" by {e['operator']}" if e.get("operator") else ""))
                if kind == "analyzed":
                    r.status, r.analysis, r.analyzed_at = IntentStatus.ANALYZED, e["analysis"], at
                elif kind == "confirmed":
                    r.status, r.confirmed_by, r.confirmed_at = IntentStatus.CONFIRMED, e["operator"], at
                elif kind in ("consumed", "cancelled", "rejected", "expired"):
                    r.status, r.outcome = IntentStatus(kind.upper()), e.get("outcome", "")
                else:
                    raise ValueError(f"unknown event {kind}")
        except (ValueError, KeyError, TypeError) as e:
            raise IntentInboxUnreadable(f"corrupt intent log: {e}") from None
        return list(recs.values())

    def get(self, intent_id: str) -> IntentRecord:
        for r in self.records():
            if r.intent_id == intent_id:
                return r
        raise IntentError(f"unknown intent {intent_id}")

    # -- the human's steps -----------------------------------------------------------
    def submit(self, symbol: str, action: IntentAction | str, reason: str, operator: str,
               at: datetime) -> str:
        symbol, reason, operator = _clean(symbol, "symbol").upper(), _clean(reason, "reason"), _clean(operator, "operator")
        if not _SYMBOL.match(symbol):
            raise IntentError(f"{symbol!r} is not a valid ticker")
        if IntentAction(action) is not IntentAction.WANT_BUY:
            raise IntentError("only WANT_BUY is supported: selling a held position would first have to cancel "
                              "its protective stop, which is not done on a wish (use the kill switch or the broker)")
        open_now = [r for r in self.records() if r.is_open]
        if len(open_now) >= MAX_OPEN_INTENTS:
            raise IntentError(f"{MAX_OPEN_INTENTS} intents are already open; cancel or finish some first")
        if any(r.symbol == symbol for r in open_now):
            raise IntentError(f"an intent for {symbol} is already open")
        seq = len(self.records()) + 1
        iid = f"HI-{at:%Y%m%d}-{seq:04d}"
        self._append({"event": "submitted", "intent_id": iid, "symbol": symbol, "action": "WANT_BUY",
                      "reason": reason, "operator": operator, "at": at.isoformat()})
        return iid

    def confirm(self, intent_id: str, operator: str, at: datetime) -> None:
        """Only after the human has had the analysis to look at (§76-77)."""
        operator = _clean(operator, "operator")
        r = self.get(intent_id)
        if r.status is not IntentStatus.ANALYZED:
            raise IntentError(f"{intent_id} is {r.status.value}: it can be confirmed only once its analysis exists "
                              f"(status ANALYZED) and has been read")
        self._append({"event": "confirmed", "intent_id": intent_id, "operator": operator, "at": at.isoformat()})

    def cancel(self, intent_id: str, operator: str, reason: str, at: datetime) -> None:
        operator, reason = _clean(operator, "operator"), _clean(reason, "reason")
        r = self.get(intent_id)
        if not r.is_open:
            raise IntentError(f"{intent_id} is already {r.status.value}")
        self._append({"event": "cancelled", "intent_id": intent_id, "operator": operator,
                      "outcome": f"cancelled by {operator}: {reason}", "at": at.isoformat()})

    # -- the system's steps -------------------------------------------------------------
    def record_analysis(self, intent_id: str, analysis: dict[str, Any], at: datetime) -> None:
        if self.get(intent_id).status is not IntentStatus.SUBMITTED:
            return                                     # analysed once; a re-analysis never unconfirms
        self._append({"event": "analyzed", "intent_id": intent_id, "analysis": analysis, "at": at.isoformat()})

    def consume(self, intent_id: str, outcome: str, at: datetime) -> None:
        self._append({"event": "consumed", "intent_id": intent_id, "outcome": outcome, "at": at.isoformat()})

    def reject(self, intent_id: str, outcome: str, at: datetime) -> None:
        self._append({"event": "rejected", "intent_id": intent_id, "outcome": outcome, "at": at.isoformat()})

    def expire_stale(self, now: datetime) -> list[str]:
        expired = []
        for r in self.records():
            if not r.is_open:
                continue
            limit = CONFIRMED_VALID_FOR if r.status is IntentStatus.CONFIRMED else UNCONFIRMED_VALID_FOR
            since = r.confirmed_at if r.status is IntentStatus.CONFIRMED else r.submitted_at
            if now - since > limit:
                self._append({"event": "expired", "intent_id": r.intent_id, "at": now.isoformat(),
                              "outcome": f"expired {limit.days} days after {r.status.value.lower()}"})
                expired.append(r.intent_id)
        return expired

    def pending_analysis(self) -> list[IntentRecord]:
        return [r for r in self.records() if r.status is IntentStatus.SUBMITTED]

    def confirmed(self) -> list[IntentRecord]:
        return [r for r in self.records() if r.status is IntentStatus.CONFIRMED]
