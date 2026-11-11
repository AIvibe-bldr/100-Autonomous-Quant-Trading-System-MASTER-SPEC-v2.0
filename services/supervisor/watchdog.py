"""Watchdog: the thing that notices when the runner cannot tell anyone (SAFETY_AUDIT F10, T7).

Runs as its OWN process (cron / systemd timer / a loop on another machine), reads the runner's status file
and the operator files, and turns what it finds into findings and — on change — alerts. `evaluate` is pure
(everything it needs is passed in), so it is testable without a clock or a filesystem.

Findings (CRITICAL needs a human now, WARNING needs a look):
  CRITICAL  no_status / status_unreadable   the runner has never reported, or its file is corrupt
  CRITICAL  heartbeat_stale                 no heartbeat for stale_after (a session in progress gets longer)
  CRITICAL  runner_failed                   the runner stopped on a failure
  CRITICAL  open_incident                   a MAJOR incident is unresolved
  WARNING   runner_stopped                  stopped on request while it should be running
  WARNING   session_overdue                 alive, market open, but today's session has not run
  WARNING   risk_not_normal                 the risk controller is halted
  WARNING   kill_switch                     the operator kill switch is engaged
  WARNING   live_gate_closed                a LIVE system without a valid human approval
  WARNING   alert_delivery / history        (reserved for the dashboard)

Alerting is edge-triggered through a small state file: a new or escalated finding alerts, a cleared one sends
a recovery note, an unchanged CRITICAL repeats only every `renotify_after`."""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from packages.common.alerts import AlertSeverity, AlertSink, notify
from packages.common.calendar import ET, SessionStatus, TradingCalendar
from packages.common.clock import ensure_utc
from services.supervisor.status_file import (
    FAILED,
    RUNNING,
    STOPPED,
    RunnerStatus,
    StatusUnreadable,
    age_seconds,
    read_status,
)


class Level(str, enum.Enum):
    OK = "OK"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_RANK = {Level.OK: 0, Level.WARNING: 1, Level.CRITICAL: 2}


@dataclass(frozen=True)
class Finding:
    level: Level
    code: str
    message: str


@dataclass(frozen=True)
class WatchdogConfig:
    stale_after: timedelta = timedelta(seconds=300)          # 5 ticks of the default 60 s
    session_stale_after: timedelta = timedelta(minutes=30)   # a session with LLM calls takes minutes
    overdue_after: timedelta = timedelta(minutes=30)         # past the session start time
    session_time_et: time = time(9, 45)
    renotify_after: timedelta = timedelta(hours=6)


@dataclass
class Inputs:
    now: datetime
    status: Optional[RunnerStatus]
    status_error: Optional[str] = None
    open_major_incidents: list[str] = field(default_factory=list)
    kill_switch_engaged: bool = False
    live_gate_open: Optional[bool] = None                    # None = not applicable (not a LIVE system)


def evaluate(i: Inputs, calendar: TradingCalendar, cfg: WatchdogConfig = WatchdogConfig(),
             expect_running: bool = True) -> list[Finding]:
    out: list[Finding] = []
    now = ensure_utc(i.now)
    st = i.status
    if i.status_error:
        out.append(Finding(Level.CRITICAL, "status_unreadable", i.status_error))
    elif st is None:
        out.append(Finding(Level.CRITICAL, "no_status", "the runner has never written a status file"))
    else:
        age = age_seconds(st, now)
        limit = cfg.session_stale_after if st.state == RUNNING else cfg.stale_after
        if st.state == FAILED:
            out.append(Finding(Level.CRITICAL, "runner_failed", f"the runner stopped on a failure: {st.failure}"))
        elif st.state == STOPPED:
            if expect_running:
                out.append(Finding(Level.WARNING, "runner_stopped", "the runner was stopped on request"))
        elif age > limit.total_seconds():
            out.append(Finding(Level.CRITICAL, "heartbeat_stale",
                               f"no heartbeat for {age / 60:.0f} min (state {st.state}, limit {limit.seconds // 60} min)"))
        else:
            out.extend(_alive_findings(st, now, calendar, cfg))
        if st.risk_state != "NORMAL":
            out.append(Finding(Level.WARNING, "risk_not_normal", f"risk state is {st.risk_state}"))
    for iid in i.open_major_incidents:
        out.append(Finding(Level.CRITICAL, "open_incident", f"unresolved MAJOR incident {iid}"))
    if i.kill_switch_engaged:
        out.append(Finding(Level.WARNING, "kill_switch", "the operator kill switch is engaged (entries halted)"))
    if i.live_gate_open is False:
        out.append(Finding(Level.WARNING, "live_gate_closed", "LIVE system without a valid human approval (no entries)"))
    return out


def _alive_findings(st: RunnerStatus, now: datetime, calendar: TradingCalendar,
                    cfg: WatchdogConfig) -> list[Finding]:
    """The runner is alive; is it doing its job today?"""
    try:
        et = now.astimezone(ET)
        due_since = datetime.combine(et.date(), cfg.session_time_et, tzinfo=ET) + cfg.overdue_after
        if (calendar.status(now) is SessionStatus.OPEN and et >= due_since
                and st.last_session_date != et.date().isoformat() and st.state != RUNNING):
            return [Finding(Level.WARNING, "session_overdue",
                            f"today's session ({et.date()}) has not run; last session {st.last_session_date or 'never'}")]
    except ValueError:                                      # the calendar has no data for this year
        return [Finding(Level.WARNING, "calendar", "the trading calendar has no data for this year")]
    return []


def worst(findings: list[Finding]) -> Level:
    return max((f.level for f in findings), key=_RANK.__getitem__, default=Level.OK)


def exit_code(findings: list[Finding]) -> int:
    return _RANK[worst(findings)]


# -- alerting on change ---------------------------------------------------------------------------
def alert_on_change(findings: list[Finding], state_path: Path, sink: AlertSink, now: datetime,
                    cfg: WatchdogConfig = WatchdogConfig()) -> list[str]:
    """Alert for new/escalated findings and send recovery notes; returns what was sent (for logs/tests)."""
    prev: dict[str, dict] = {}
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    now = ensure_utc(now)
    sent: list[str] = []
    current = {f.code: f for f in findings}
    keep: dict[str, dict] = {}
    for code, f in current.items():
        old = prev.get(code)
        last = datetime.fromisoformat(old["at"]) if old else None
        escalated = old is not None and _RANK[Level(f.level)] > _RANK[Level(old["level"])]
        repeat = (f.level is Level.CRITICAL and last is not None and now - last >= cfg.renotify_after)
        if old is None or escalated or repeat:
            notify(sink, AlertSeverity.CRITICAL if f.level is Level.CRITICAL else AlertSeverity.WARNING,
                   f"watchdog: {f.code} — {f.message}")
            sent.append(code)
            keep[code] = {"level": f.level.value, "at": now.isoformat()}
        else:
            keep[code] = old
    for code in prev.keys() - current.keys():
        notify(sink, AlertSeverity.WARNING, f"watchdog: recovered — {code}")
        sent.append(f"recovered:{code}")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(keep), encoding="utf-8")
    return sent


def gather(status_path: Path, incidents, kill_switch, live_gate_status: Optional[bool],
           now: datetime) -> Inputs:
    """Read the files the watchdog looks at. Anything unreadable becomes a finding, never an exception."""
    status, error = None, None
    try:
        status = read_status(status_path)
    except StatusUnreadable as e:
        error = str(e)
    majors = [i["incident_id"] for i in incidents.open_major()]
    return Inputs(now=now, status=status, status_error=error, open_major_incidents=majors,
                  kill_switch_engaged=kill_switch.status().engaged, live_gate_open=live_gate_status)
