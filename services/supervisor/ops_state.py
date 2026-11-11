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
from packages.common.durable_store import DurableCheckpointStore, DurableSessionStore, SessionRecord
from packages.common.environment import Environment
from packages.common.kill_switch import KillSwitch
from packages.common.live_gate import LiveGate
from packages.common.symbol_profiles import profile_of
from services.decision.intent_inbox import IntentInbox, IntentInboxUnreadable
from services.supervisor.incident_log import IncidentLog
from services.supervisor.performance_report import PROPOSED_MIN_SESSIONS, summarize
from services.supervisor.status_file import FILE_NAME, StatusUnreadable, read_status
from services.supervisor.watchdog import Inputs, WatchdogConfig, evaluate, worst


# Which holdings to put in front of a human first. These are display heuristics, not trading rules: nothing
# here changes an order, a stop or a limit.
NEAR_STOP = 0.03          # price within 3% above its protective stop
NEAR_TARGET = 0.03        # price within 3% below its profit target
BIG_MOVE = 0.03           # up or down 3% from the buying price
HEAVY_WEIGHT = 0.20       # one name is 20% or more of the equity
ATTENTION_COUNT = 10


def attention_of(h: dict[str, Any]) -> tuple[float, list[str]]:
    """(score, reasons in plain words) for one holding. Pure. A higher score is looked at first."""
    score, reasons = 0.0, []
    plan = h.get("plan")
    if plan is None:
        score += 5
        reasons.append("計画の記録がありません")
    else:
        stop, target, price = plan["stop_price"], plan["profit_target"], h["price"]
        gap = price / stop - 1.0 if stop > 0 else 1.0
        if gap <= NEAR_STOP:
            score += 4 + (NEAR_STOP - gap)
            reasons.append(f"損切りラインまであと {gap * 100:.1f}%")
        if target > 0 and price >= target * (1 - NEAR_TARGET):
            score += 3
            reasons.append("利益の目標に近い")
    if abs(h["pnl_pct"]) >= BIG_MOVE:
        score += 2 + abs(h["pnl_pct"])
        reasons.append(("大きく上がっています" if h["pnl_pct"] > 0 else "含み損が大きくなっています") + f"（{h['pnl_pct'] * 100:+.1f}%）")
    if h["weight"] >= HEAVY_WEIGHT:
        score += 1 + h["weight"]
        reasons.append(f"資産の {h['weight'] * 100:.0f}% を占めています")
    return score, reasons


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

    def holdings(self) -> dict[str, Any]:
        """The positions held at the end of the latest recorded session, with P&L and the plan made before entry.
        Values are as of that session, not live; sessions recorded before this field existed say so."""
        try:
            recs = self._records()
        except Exception as e:  # noqa: BLE001
            return {"error": f"session database unreadable: {type(e).__name__}", "holdings": []}
        if not recs:
            return {"session_date": None, "holdings": [], "recorded": False}
        last = recs[-1]
        detail = json.loads(last.detail_json)
        items = detail.get("holdings")
        out = []
        for h in items or []:
            prof, auto = profile_of(h["symbol"]), h.get("profile_auto")
            score, reasons = attention_of(h)
            names = ({"company": prof.company, "company_ja": prof.company_ja, "category": prof.category,
                      "business": prof.business, "profile_source": "table"} if prof else
                     {"company": auto["company"], "company_ja": None, "category": auto["category"],
                      "business": auto["business"], "profile_source": "sec"} if auto else
                     {"company": None, "company_ja": None, "category": None, "business": None, "profile_source": None})
            out.append({**h, **names,
                        "attention_score": round(score, 4), "attention_reasons": reasons})
        order = sorted(out, key=lambda x: (-x["attention_score"], -abs(x["pnl_pct"]), x["symbol"]))
        top = [x["symbol"] for x in order[:ATTENTION_COUNT]]
        return {"session_date": last.session_date, "recorded": items is not None, "cash": last.cash,
                "equity": last.equity, "attention": top, "holdings": out}

    def checkpoint(self) -> Optional[dict[str, Any]]:
        """When the account was last saved, and why (read-only; the state itself is not parsed)."""
        path = self.state_dir / "checkpoints.db"
        if not path.exists():
            return None
        try:
            return DurableCheckpointStore(str(path), Environment.PAPER, read_only=True).info()
        except Exception as e:  # noqa: BLE001
            return {"error": f"checkpoint database unreadable: {type(e).__name__}"}

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
            "checkpoint": self.checkpoint(),
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
