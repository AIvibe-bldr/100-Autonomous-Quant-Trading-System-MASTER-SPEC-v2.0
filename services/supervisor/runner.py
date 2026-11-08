"""Session runner (SAFETY_AUDIT F10 / docs/paper_live_gate.md T7).

Runs one trading session per trading day and reports that it is alive. It owns three things
and nothing else: WHEN a session starts (trading day, market open, after a configured time,
at most once per date), the HEARTBEATS that make a stalled or failed system visible, and
what happens when a session blows up. Trading decisions stay in `TradingPipeline`.

Failure policy (fail closed, never swallowed): an exception out of `run_session` raises a
CRITICAL alert, moves the risk controller to HALT_NEW_ENTRIES, marks the pipeline ERROR and
STOPS the runner. A human looks first; nothing restarts itself into the same fault.

Heartbeats: the staleness rule (5 min) means a once-a-day pipeline would always look dead,
so the runner re-reports the pipeline's latest outcome on every tick (`at` = now,
`last_success` = the last good session). A runner that stops ticking goes stale, which
shows as ERROR in the registry (e.g. the status API's /health); because the alerter is
driven by the runner's own ticks, a dead runner cannot alert about itself — an external
watcher on the registry (or the process supervisor) is needed for that."""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional

from packages.common.alerts import AlertSeverity, AlertSink, notify
from packages.common.calendar import ET, SessionStatus, TradingCalendar
from packages.common.clock import Clock, ensure_utc
from services.pipeline import PipelineResult, TradingPipeline
from services.risk.master_controller import RiskState
from services.supervisor.heartbeat import Heartbeat, HeartbeatRegistry, ServiceStatus
from services.supervisor.heartbeat_alerts import HeartbeatAlerter
from services.supervisor.incident_log import IncidentLog
from services.supervisor.recovery import FaultSeverity, RecoveryManager

log = logging.getLogger("quant.runner")


@dataclass(frozen=True)
class RunnerConfig:
    # a session starts at the first tick at/after this ET time on a trading day (the market is
    # open and the opening-auction noise has passed)
    session_time_et: time = time(9, 45)
    tick_seconds: float = 60.0              # heartbeat cadence; must stay well under the 5 min stale limit

    def __post_init__(self) -> None:
        if not (0 < self.tick_seconds <= 120):
            raise ValueError("tick_seconds must be in (0, 120] so heartbeats never go stale")


def session_due(calendar: TradingCalendar, now: datetime, last_run: Optional[date],
                session_time_et: time) -> Optional[date]:
    """The trading date whose session should start now, else None. Pure.

    Due only while the market is OPEN, at/after the configured time, and not already run
    for that date — so a runner started after the close does not run a session on stale
    quotes, and a restart within the day does not run a second one."""
    now_et = ensure_utc(now).astimezone(ET)
    d = now_et.date()
    if last_run == d:
        return None
    if calendar.status(now) is not SessionStatus.OPEN or now_et.time() < session_time_et:
        return None
    return d


def next_session_at(calendar: TradingCalendar, after: datetime, last_run: Optional[date],
                    session_time_et: time) -> datetime:
    """When the next session is scheduled to start (UTC), for display and tests. Pure."""
    d = ensure_utc(after).astimezone(ET).date()
    for _ in range(14):
        if calendar.is_trading_day(d) and d != last_run:
            at = datetime.combine(d, session_time_et, tzinfo=ET)
            if at > ensure_utc(after):
                return at.astimezone(ensure_utc(after).tzinfo)
        d += timedelta(days=1)
    raise RuntimeError("no trading day within 14 days")      # a calendar problem, not a quiet week


class SessionRunner:
    def __init__(self, pipeline: TradingPipeline, calendar: TradingCalendar, clock: Clock,
                 heartbeats: HeartbeatRegistry, sink: AlertSink,
                 config: RunnerConfig = RunnerConfig(),
                 sleep: Callable[[float], None] = _time.sleep,
                 after_session: Optional[Callable[[datetime, PipelineResult], None]] = None,
                 recovery: Optional[RecoveryManager] = None,
                 incident_log: Optional[IncidentLog] = None) -> None:
        self._pipeline = pipeline
        self._calendar = calendar
        self._clock = clock
        self._hb = heartbeats
        self._sink = sink
        self._alerter = HeartbeatAlerter(heartbeats, sink)
        self._cfg = config
        self._sleep = sleep
        self._after = after_session
        self._recovery = recovery
        self._incident_log = incident_log
        self.last_run: Optional[date] = None
        self.failed: Optional[str] = None
        self._pipeline_status = ServiceStatus.OK
        self._last_good: Optional[datetime] = None

    # -- one tick ------------------------------------------------------------
    def tick(self) -> Optional[PipelineResult]:
        """Report liveness, raise heartbeat alerts, and run the session if one is due."""
        now = self._clock.now()
        self._beat("runner", ServiceStatus.OK, now, now)
        if self._last_good is not None or self.failed:
            self._beat("pipeline", self._pipeline_status, now, self._last_good or now)
        self._alerter.check()
        try:
            due = session_due(self._calendar, now, self.last_run, self._cfg.session_time_et)
        except ValueError as e:                      # calendar has no data for this year
            self._fail(now, f"trading calendar unavailable: {e}")
            return None
        return self._run(now, due) if due else None

    def _run(self, now: datetime, session_date: date) -> Optional[PipelineResult]:
        log.info("session start date=%s", session_date)
        try:
            result = self._pipeline.run_session(now)
        except Exception as e:  # noqa: BLE001 — fail closed: alert, halt entries, stop the runner
            self._fail(now, f"session {session_date} raised {type(e).__name__}: {e}")
            return None
        self.last_run = session_date
        self._last_good = now
        self._pipeline_status = ServiceStatus.WARNING if result.data_warnings else ServiceStatus.OK
        self._beat("pipeline", self._pipeline_status, now, now)
        if result.data_warnings:
            self._minor_fault(now, "; ".join(result.data_warnings)[:300])
        log.info("session done date=%s scanned=%d candidates=%d filled=%d warnings=%d stops=%d",
                 session_date, result.scanned, result.candidates, result.orders_filled,
                 len(result.data_warnings), result.stops_triggered)
        if self._after is not None:
            try:
                self._after(now, result)
            except Exception as e:  # noqa: BLE001 — losing the record of a session is a failure too
                self._fail(now, f"after-session step failed: {type(e).__name__}: {e}")
        return result

    def _minor_fault(self, now: datetime, description: str) -> None:
        """§69: a minor fault is recorded and may auto-resume (the session already carried on)."""
        if self._recovery is None or self._incident_log is None:
            return
        inc = self._recovery.report_fault("data", description, FaultSeverity.MINOR)
        self._incident_log.record_fault(inc, self._recovery.postmortem(inc))
        self._recovery.resume(inc)
        self._incident_log.record_resolution(inc.incident_id, "auto",
                                             "minor fault; auto-resume allowed (§69)", now,
                                             human_approved=False)

    def _fail(self, now: datetime, reason: str) -> None:
        self.failed = reason
        if self._recovery is not None and self._incident_log is not None:
            # §69: a major fault is NOT auto-resumed. It stays open in the durable log until a
            # human resolves it, and a new runner refuses to start while it is open.
            inc = self._recovery.report_fault("runner", reason, FaultSeverity.MAJOR)
            self._incident_log.record_fault(inc, self._recovery.postmortem(inc))
        self._pipeline_status = ServiceStatus.ERROR
        self._beat("pipeline", ServiceStatus.ERROR, now, self._last_good or now)
        self._pipeline.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES,
                                                 reason=f"runner failure: {reason}")
        notify(self._sink, AlertSeverity.CRITICAL, f"runner stopped: {reason}")
        self._alerter.check()
        log.error("runner stopped: %s", reason)

    def _beat(self, service: str, status: ServiceStatus, now: datetime, last_success: datetime) -> None:
        self._hb.report(Heartbeat(service=service, at=now, status=status, last_success=last_success))

    # -- the loop --------------------------------------------------------------
    def run_forever(self, should_stop: Callable[[], bool] = lambda: False) -> Optional[str]:
        """Tick until `should_stop()` or a failure. Returns the failure reason, or None for
        a requested stop."""
        while not should_stop():
            self.tick()
            if self.failed:
                return self.failed
            self._sleep(self._cfg.tick_seconds)
        return None
