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

from packages.common.alerts import LoggingAlertSink
from packages.common.calendar import TradingCalendar
from packages.common.clock import Clock
from packages.common.durable_store import (
    DurableAuditStore,
    DurableOrderStore,
    DurablePitStore,
    DurableSessionStore,
)
from packages.common.environment import Environment
from packages.common.kill_switch import KillSwitch, describe
from services.composition import DEFAULT_SYMBOLS, build_paper_pipeline, build_real_data_sources
from services.market_data.service import MarketDataUnavailable
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.forecast_observer import ForecastObserver
from services.pdca.pit_observer import PitObserver
from services.supervisor.heartbeat import HeartbeatRegistry
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
    parser.add_argument("--model-tag", default="mock-ai",
                        help="label stored with each session so runs can be compared")
    args = parser.parse_args(argv)

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

    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    order_store = DurableOrderStore(str(state / "orders.db"), Environment.PAPER)
    session_store = DurableSessionStore(str(state / "sessions.db"), Environment.PAPER)
    if order_store.all() or session_store.all():
        print(f"refusing to start: {state} holds orders/sessions from an earlier run, but the "
              f"paper broker/ledger cannot be restored (a restart is a new account). "
              f"Use a new --state-dir.", file=sys.stderr)
        return 2

    provider = news = events = flow_filings = None
    if args.real_data:
        try:
            src = build_real_data_sources()
        except MarketDataUnavailable as e:
            print(f"--real-data: {e} (set APCA_API_KEY_ID and APCA_API_SECRET_KEY)", file=sys.stderr)
            return 2
        provider, news, events = src.provider, src.news_source, src.event_source
        flow_filings = src.flow_filings_source
        for note in src.notices:
            print(f"({note})")

    universe = UniverseManager()
    for s in DEFAULT_SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    kill_switch = KillSwitch.from_env()
    pipeline = build_paper_pipeline(
        clock, universe, initial_cash=args.cash, provider=provider, news_source=news,
        event_source=events, flow_filings_source=flow_filings, kill_switch=kill_switch,
        order_store=order_store,
        observers=[ForecastObserver(), PitObserver(DurablePitStore(str(state / "pit.db"), Environment.PAPER))],
        audit_store=DurableAuditStore(str(state / "audit.db"), Environment.PAPER))
    sink = LoggingAlertSink()
    pipeline.risk_controller.alert_sink = sink
    broker = pipeline.execution.broker

    stop = threading.Event()            # set by SIGINT/SIGTERM; its wait() is the tick sleep, so a
    for sig in (signal.SIGINT, signal.SIGTERM):      # stop request ends the wait at once
        signal.signal(sig, lambda *_: stop.set())
    recorder = SessionRecorder(pipeline, session_store, args.model_tag)

    def after_session(at, result):
        recorder(at, result)             # record first: a failure here stops the runner
        broker.settle()                  # paper T+1 tick

    runner = SessionRunner(pipeline, cal, clock, HeartbeatRegistry(clock), sink, cfg,
                           sleep=stop.wait, after_session=after_session)

    last_year = getattr(cal.provider, "last_year", None)       # not every provider knows its range
    if last_year is not None and clock.now().year >= last_year and clock.now().month >= 10:
        logging.getLogger("quant.runner").warning(
            "trading calendar data ends in %d: add the next year (NYSE announcement) before "
            "1 January or the runner will stop closed", last_year)
    logging.getLogger("quant.runner").info(
        "runner up: PAPER, cash $%.2f, data=%s, kill switch %s, state %s",
        args.cash, "alpaca" if provider else "mock", describe(kill_switch.status()), state)
    failure = runner.run_forever(stop.is_set)
    if failure:
        print(f"runner stopped on failure: {failure}", file=sys.stderr)
        return 1
    print("runner stopped on request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
