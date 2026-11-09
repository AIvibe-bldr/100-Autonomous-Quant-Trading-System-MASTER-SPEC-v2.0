"""Everything the ops dashboard shows, read from the files a RUNNING system leaves behind.

The dashboard runs in its own process, so it cannot ask the runner anything: it reads the state directory
(runner status file, session database, alert history) and the operator files (incident log, intent log, kill
switch, LIVE gate). All of it is READ-ONLY by construction: the session database is opened in SQLite's
read-only mode, and no function here writes anything. Anything unreadable is reported as an error in its own
section, never as an empty list that would read as "all clear"."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from packages.common.alert_sinks import read_alert_history
from packages.common.calendar import TradingCalendar
from packages.common.durable_store import DurableSessionStore, SessionRecord
from packages.common.environment import Environment
from packages.common.kill_switch import KillSwitch
from packages.common.live_gate import LiveGate
from services.decision.intent_inbox import IntentInbox, IntentInboxUnreadable
from services.supervisor.incident_log import IncidentLog
from services.supervisor.performance_report import PROPOSED_MIN_SESSIONS, summarize
from services.supervisor.status_file import FILE_NAME, StatusUnreadable, read_status
from services.supervisor.watchdog import Inputs, WatchdogConfig, evaluate, worst


def _public(rec: SessionRecord) -> dict[str, Any]:
    d = asdict(rec)
    d.pop("detail_json")
    d["recorded_at"] = rec.recorded_at.isoformat()
    return d


class OpsState:
    def __init__(self, state_dir: Path | str, incidents: Optional[IncidentLog] = None,
                 intents: Optional[IntentInbox] = None, kill_switch: Optional[KillSwitch] = None,
                 live_gate: Optional[LiveGate] = None, calendar: Optional[TradingCalendar] = None) -> None:
        self.state_dir = Path(state_dir)
        self.incidents_log = incidents or IncidentLog.from_env()
        self.intents_log = intents or IntentInbox.from_env()
        self.kill_switch = kill_switch or KillSwitch.from_env()
        self.live_gate = live_gate or LiveGate.from_env()
        self.calendar = calendar or TradingCalendar()

    # -- sessions ------------------------------------------------------------------------
    def _records(self) -> list[SessionRecord]:
        path = self.state_dir / "sessions.db"
        if not path.exists():
            return []
        return DurableSessionStore(str(path), Environment.PAPER, read_only=True).all()

    def sessions(self, limit: int = 60) -> dict[str, Any]:
        try:
            recs = self._records()
        except Exception as e:  # noqa: BLE001 — a locked/corrupt database is shown, not hidden
            return {"error": f"session database unreadable: {type(e).__name__}", "sessions": []}
        return {"sessions": [_public(r) for r in recs[-limit:]][::-1], "total": len(recs)}

    def session_detail(self, session_date: str) -> Optional[dict[str, Any]]:
        for r in self._records():
            if r.session_date == session_date:
                return {**_public(r), "detail": json.loads(r.detail_json)}
        return None

    def performance(self) -> dict[str, Any]:
        try:
            recs = self._records()
        except Exception as e:  # noqa: BLE001
            return {"error": f"session database unreadable: {type(e).__name__}", "sessions": 0}
        if not recs:
            return {"sessions": 0, "proposed_min_sessions": PROPOSED_MIN_SESSIONS, "series": [], "summary": None}
        s = summarize(recs)
        return {
            "sessions": s.sessions, "proposed_min_sessions": PROPOSED_MIN_SESSIONS,
            "enough_sessions": s.enough_sessions, "summary": asdict(s),
            "series": [{"date": r.session_date, "equity": r.equity, "drawdown": r.drawdown,
                        "day_pnl": r.day_pnl, "risk_state": r.risk_state} for r in recs],
            "warning": None if s.enough_sessions else
            f"{s.sessions}/{PROPOSED_MIN_SESSIONS} sessions: too few to say anything about profitability",
        }

    def observers(self) -> dict[str, Any]:
        try:
            recs = self._records()
        except Exception as e:  # noqa: BLE001
            return {"error": f"session database unreadable: {type(e).__name__}"}
        if not recs:
            return {"session_date": None, "observers": {}}
        last = recs[-1]
        detail = json.loads(last.detail_json)
        return {"session_date": last.session_date, "observers": detail.get("observers", {}),
                "disagreement_multipliers": detail.get("disagreement_multipliers", {}),
                "no_trade_reasons": detail.get("no_trade_reasons", []),
                "data_warnings": detail.get("data_warnings", []), "fills": detail.get("fills", [])}

    # -- the runner and the operator files --------------------------------------------------
    def status(self, now: datetime) -> dict[str, Any]:
        status, error = None, None
        try:
            status = read_status(self.state_dir / FILE_NAME)
        except StatusUnreadable as e:
            error = str(e)
        try:
            majors = [i["incident_id"] for i in self.incidents_log.open_major()]
        except Exception as e:  # noqa: BLE001
            majors = [f"incident log unreadable: {type(e).__name__}"]
        ks = self.kill_switch.status()
        live_open = None
        if status is not None and status.environment == "LIVE":
            live_open = self.live_gate.status(now).is_open
        findings = evaluate(Inputs(now=now, status=status, status_error=error, open_major_incidents=majors,
                                   kill_switch_engaged=ks.engaged, live_gate_open=live_open),
                            self.calendar, WatchdogConfig())
        return {
            "now": now.isoformat(), "overall": worst(findings).value,
            "findings": [{"level": f.level.value, "code": f.code, "message": f.message} for f in findings],
            "runner": asdict(status) if status else None,
            "kill_switch": {"engaged": ks.engaged, "operator": ks.operator, "reason": ks.reason, "at": ks.at},
            "live_gate": None if live_open is None else {"open": live_open},
            "open_major_incidents": majors,
        }

    def incidents(self) -> dict[str, Any]:
        try:
            items = self.incidents_log.incidents()
        except Exception as e:  # noqa: BLE001
            return {"error": f"incident log unreadable: {type(e).__name__}", "incidents": []}
        return {"incidents": [{k: v for k, v in i.items() if k != "postmortem"} for i in reversed(items)]}

    def intents(self) -> dict[str, Any]:
        try:
            recs = self.intents_log.records()
        except IntentInboxUnreadable as e:
            return {"error": str(e), "intents": []}
        return {"intents": [{"intent_id": r.intent_id, "symbol": r.symbol, "status": r.status.value,
                             "reason": r.stated_reason, "submitted_by": r.submitted_by,
                             "submitted_at": r.submitted_at.isoformat(), "confirmed_by": r.confirmed_by,
                             "outcome": r.outcome, "analysis": r.analysis, "history": r.history}
                            for r in reversed(recs)]}

    def alerts(self, limit: int = 50) -> dict[str, Any]:
        return {"alerts": read_alert_history(self.state_dir / "alerts.jsonl", limit)}
