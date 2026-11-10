"""Paper session runner: one session per US trading day, with heartbeats and alerts.

  python3 scripts/run_paper_runner.py --show-schedule            # next sessions, no trading
  python3 scripts/run_paper_runner.py --real-data                # run until stopped (Ctrl-C / SIGTERM)

Orders go to the in-memory PaperBroker only; nothing is sent to a real broker.
It uses the deterministic mock Decision/Skeptic/Audit AI. The real LLM stack is NOT wired
here on purpose: LLM spending needs your explicit instruction, and the spending cap
(QUANT_LLM_BUDGET_USD) is per process, which is not a per-day cap for a long-running runner.

Restart limits (read this): the paper broker and ledger live in memory, so a restart starts a
fresh account. The durable order store would then describe orders the new broker has never
seen, so the runner REFUSES to start on a non-empty store — point --state-dir at a new
directory (or archive the old one) to begin a new run. A failed session halts new entries,
sends a CRITICAL alert and stops the runner; it never restarts itself.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.alert_sinks import alert_sink_from_env, describe_sink
from packages.common.alerts import AlertSeverity, notify
from packages.common.calendar import TradingCalendar
from packages.common.clock import Clock
from packages.common.durable_store import (
    DurableAuditStore,
    DurableCheckpointStore,
    DurableOrderStore,
    DurablePitStore,
    DurableSessionStore,
)
from packages.common.environment import Environment
from packages.common.kill_switch import KillSwitch, describe
from services.checkpoint import CheckpointError, Checkpointer, restore_latest
from services.composition import (
    DEFAULT_SYMBOLS,
    attach_learning_observers,
    build_paper_pipeline,
    build_real_data_sources,
)
from services.market_data.service import MarketDataUnavailable
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.decision.intent_inbox import IntentInbox
from services.risk.master_controller import RiskState
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.incident_log import IncidentLog
from services.supervisor.recovery import FaultSeverity, RecoveryManager
from services.supervisor.status_file import FILE_NAME as STATUS_FILE
from services.supervisor.status_file import write_status
from services.supervisor.runner import RunnerConfig, SessionRunner, next_session_at
from services.supervisor.session_record import SessionRecorder

JST = ZoneInfo("Asia/Tokyo")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--real-data", action="store_true",
                        help="Alpaca market data/news/events (+EDGAR if QUANT_SEC_USER_AGENT is set)")
    parser.add_argument("--cash", type=float, default=670.0)
    parser.add_argument("--state-dir", default="var/runner")
    parser.add_argument("--show-schedule", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="continue the account saved in --state-dir (restored and verified; refuses if it "
                             "cannot be). Without it, a state dir that already holds an account is refused.")
    parser.add_argument("--resume-if-exists", action="store_true",
                        help="for a service manager: continue the account in --state-dir if there is one, "
                             "otherwise start a new one")
    parser.add_argument("--clear-halt", action="store_true",
                        help="with --resume: set a restored HALT back to NORMAL. A halt SURVIVES a restart; only a "
                             "named human clears it (needs --operator and --reason; recorded in the incident log)")
    parser.add_argument("--operator", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--model-tag", default="mock-ai",
                        help="label stored with each session so runs can be compared")
    args = parser.parse_args(argv)

    if args.clear_halt and not (args.resume and args.operator.strip() and args.reason.strip()):
        print("--clear-halt needs --resume, --operator NAME and --reason TEXT", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg, cal, clock = RunnerConfig(), TradingCalendar(), Clock()

    if args.show_schedule:
        at, last = clock.now(), None
        print(f"session start {cfg.session_time_et} ET on US trading days (market must be open):")
        for _ in range(5):
            at = next_session_at(cal, at, last, cfg.session_time_et)
            print(f"  {at.astimezone(ZoneInfo('America/New_York')):%a %Y-%m-%d %H:%M ET}"
                  f"  =  {at.astimezone(JST):%a %m-%d %H:%M JST}")
        return 0

    incidents = IncidentLog.from_env()
    open_major = incidents.open_major()
    if open_major:                                           # §69: no auto-resume after a major fault
        print("refusing to start: unresolved MAJOR incident(s): "
              + ", ".join(i["incident_id"] for i in open_major)
              + "\nReview them with scripts/incidents.py, then resolve with a named operator and reason.",
              file=sys.stderr)
        return 2

    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    order_store = DurableOrderStore(str(state / "orders.db"), Environment.PAPER)
    session_store = DurableSessionStore(str(state / "sessions.db"), Environment.PAPER)
    ckpt_store = DurableCheckpointStore(str(state / "checkpoints.db"), Environment.PAPER)
    has_account = bool(order_store.all() or session_store.all() or ckpt_store.info())
    if args.resume_if_exists and has_account:
        args.resume = True
    if has_account and not args.resume:
        print(f"refusing to start: {state} already holds an account. Add --resume to continue it "
              f"(it is restored and verified), or use a new --state-dir for a new account.", file=sys.stderr)
        return 2
    if args.resume and not has_account:
        print(f"nothing to resume in {state}: no orders, sessions or checkpoints.", file=sys.stderr)
        return 2

    provider = news = events = flow_filings = company_lookup = None
    if args.real_data:
        try:
            src = build_real_data_sources()
        except MarketDataUnavailable as e:
            print(f"--real-data: {e} (set APCA_API_KEY_ID and APCA_API_SECRET_KEY)", file=sys.stderr)
            return 2
        provider, news, events = src.provider, src.news_source, src.event_source
        flow_filings = src.flow_filings_source
        company_lookup = src.company_lookup
        for note in src.notices:
            print(f"({note})")

    universe = UniverseManager()
    for s in DEFAULT_SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    kill_switch = KillSwitch.from_env()
    try:
        sink = alert_sink_from_env(state / "alerts.jsonl")        # logging + history file + webhook/e-mail if configured
    except ValueError as e:
        print(f"alert configuration: {e}", file=sys.stderr)
        return 2
    pipeline = build_paper_pipeline(
        clock, universe, initial_cash=args.cash, provider=provider, news_source=news,
        event_source=events, flow_filings_source=flow_filings, kill_switch=kill_switch,
        order_store=order_store, human_inbox=IntentInbox.from_env(),
        audit_store=DurableAuditStore(str(state / "audit.db"), Environment.PAPER))
    pipeline.risk_controller.alert_sink = sink
    checkpointer = Checkpointer(pipeline, ckpt_store)
    resumed = None
    if args.resume:
        try:
            resumed = restore_latest(pipeline, ckpt_store)
        except CheckpointError as e:                              # a human decides: new account, or repair
            print(f"cannot resume {state}: {e}", file=sys.stderr)
            return 2
        logging.getLogger("quant.runner").info(
            "resumed account from checkpoint %d (%s, %s): cash $%.2f, %d position(s)", resumed.checkpoint_id,
            resumed.checkpoint_at, resumed.reason, pipeline.ledger.cash, len(pipeline.ledger.positions))
    if args.clear_halt and pipeline.risk_controller.state.value != "NORMAL":
        previous = pipeline.risk_controller.state.value
        pipeline.risk_controller.set_state(RiskState.NORMAL, reason="cleared by operator")
        note = f"risk state {previous} cleared on resume by {args.operator.strip()}: {args.reason.strip()}"
        rm = RecoveryManager(clock, start_seq=incidents.next_seq())
        inc = rm.report_fault("risk", note, FaultSeverity.MINOR)
        incidents.record_fault(inc, rm.postmortem(inc))
        incidents.record_resolution(inc.incident_id, args.operator, args.reason, clock.now(), human_approved=True)
        notify(sink, AlertSeverity.WARNING, note)
        print(note)
    pipeline.checkpointer = checkpointer
    last_sessions = session_store.all()
    resumed_equity = None
    if resumed is not None:
        resumed_equity = session_store.latest_equity() or pipeline.ledger.initial_cash
    attach_learning_observers(pipeline, pipeline.ledger.initial_cash if resumed else args.cash,
                              pit_store=DurablePitStore(str(state / "pit.db"), Environment.PAPER),
                              resumed_equity=resumed_equity)
    checkpointer("resumed" if resumed else "startup")             # the account exists on disk from the first moment
    broker = pipeline.execution.broker

    stop = threading.Event()            # set by SIGINT/SIGTERM; its wait() is the tick sleep, so a
    for sig in (signal.SIGINT, signal.SIGTERM):      # stop request ends the wait at once
        signal.signal(sig, lambda *_: stop.set())
    recorder = SessionRecorder(pipeline, session_store, args.model_tag, company_lookup=company_lookup)
    resumed_last_run = None
    if resumed is not None:
        if last_sessions:
            resumed_last_run = date.fromisoformat(last_sessions[-1].session_date)
        if resumed.interrupted_session is not None:     # it began and did not finish: not run a second time
            resumed_last_run = max(resumed_last_run or resumed.interrupted_session, resumed.interrupted_session)

    def after_session(at, result):
        recorder(at, result)             # record first: a failure here stops the runner
        broker.settle()                  # paper T+1 tick

    runner = SessionRunner(pipeline, cal, clock, HeartbeatRegistry(clock), sink, cfg,
                           sleep=stop.wait, after_session=after_session,
                           recovery=RecoveryManager(clock, start_seq=incidents.next_seq()),
                           incident_log=incidents,
                           market_open_hook=pipeline.analyze_human_intents,
                           status_sink=lambda st: write_status(state / STATUS_FILE, st),
                           checkpoint=checkpointer, initial_last_run=resumed_last_run)

    last_year = getattr(cal.provider, "last_year", None)       # not every provider knows its range
    if last_year is not None and clock.now().year >= last_year and clock.now().month >= 10:
        logging.getLogger("quant.runner").warning(
            "trading calendar data ends in %d: add the next year (NYSE announcement) before "
            "1 January or the runner will stop closed", last_year)
    if resumed is not None:
        if resumed.lost_orders:
            runner.record_minor_fault(
                f"resume rewound {len(resumed.lost_orders)} order(s) the checkpoint never saw: "
                + ", ".join(resumed.lost_orders))
        if resumed.interrupted_session is not None:
            runner.record_minor_fault(f"the session of {resumed.interrupted_session} was interrupted by a "
                                      f"restart; it is not run again")
    logging.getLogger("quant.runner").info(
        "runner up: PAPER, cash $%.2f, data=%s, kill switch %s, state %s, alerts -> %s",
        args.cash, "alpaca" if provider else "mock", describe(kill_switch.status()), state, describe_sink(sink))
    failure = runner.run_forever(stop.is_set)
    if failure:
        print(f"runner stopped on failure: {failure}", file=sys.stderr)
        return 1
    print("runner stopped on request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
