"""Continuously-running PAPER dashboard.

scripts/run_dashboard.py replays a fixed number of PAST days once and then
serves a static dashboard forever. This script instead keeps ONE
TradingPipeline alive for the life of the process and advances it one
trading-calendar session at a time on a fixed real-world interval, while
serving the dashboard the whole time — so a user can leave this running and
watch results accumulate in real time, instead of re-running a script.

State is in-memory only, same as every other script in this repo (no full
ledger/decision-quality persistence layer exists yet — docs/database.md's
15-table V1 schema is separate, future work, not something this script
papers over): stopping the process loses the session history; restarting
starts a fresh PAPER run from --cash.

Environment is always PAPER (packages.common.environment) — no real broker
adapter exists in this repo (packages/broker_adapters has PaperBroker only),
so no real money is ever at risk here regardless of flags.

Usage:
    python3 scripts/run_live_dashboard.py [--port 8000] [--interval 30]
                                          [--cash 670] [--start 2026-08-10]

Decision/Skeptic/Audit AI default to the deterministic Mock stack — pass
--llm real to use the actual Claude/OpenAI APIs instead (requires
credentials; unlike run_llm_paper_demo.py this is opt-in, not
auto-detected, since a continuously-running process left overnight would
otherwise make an unbounded, unexpected number of real API calls the
moment credentials happen to be present in the environment). Either way
the environment badge stays PAPER and no broker order is ever real.
"""
from __future__ import annotations

import argparse
import sys
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from apps.api.main import create_app
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.llm_client import DEFAULT_MODEL_CONFIG, credentials_available
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pipeline import TradingPipeline
from services.reconciliation.engine import ReconciliationEngine
from tests.conftest import SYMBOLS, build_pipeline


def _build_pipeline(cash: float, universe: UniverseManager, clock: FrozenClock,
                    use_real_llm: bool) -> TradingPipeline:
    if use_real_llm:
        if not credentials_available():
            raise SystemExit(
                "--llm real requires Anthropic credentials (MY_ANTHROPIC_API_KEY or "
                "`ant auth login`) — none found. Omit --llm real to use the Mock stack.")
        from services.decision.claude_adapters import build_llm_stack

        cfg = DEFAULT_MODEL_CONFIG
        decision, skeptic, auditor = build_llm_stack(config=cfg)
        pipeline = build_pipeline(clock, universe, initial_cash=cash,
                                  decision_model=decision, skeptic_model=skeptic,
                                  auditor=auditor)
        print(f"=== LIVE LLM APIs === Decision AI: {decision.name} "
              f"| Skeptic/Audit AI: {cfg.skeptic.agent_id} "
              f"(each session makes real, billed API calls)")
    else:
        pipeline = build_pipeline(clock, universe, initial_cash=cash)
        print("=== deterministic Mock Decision/Skeptic/Audit AI "
              "(pass --llm real to use the real Claude/OpenAI APIs instead) ===")
    return pipeline


def _print_session_result(d: date, result) -> None:
    print(f"\n--- session {d.isoformat()} ---")
    print(f"funnel: scanned {result.scanned} -> candidates {result.candidates} -> "
          f"decisions {result.decision_candidates} -> risk_passed {result.risk_passed} "
          f"-> filled {result.orders_filled}")
    for f in result.fills:
        print(f"  FILL {f.side.value} {f.qty:g} {f.symbol} @ {f.price:.2f}")
    if not result.traded:
        for r in result.no_trade_reasons[:3]:
            print(f"    · {r}")


def advance_one_session(pipeline: TradingPipeline, calendar: TradingCalendar,
                        clock: FrozenClock, app) -> None:
    """One simulated trading day: run a session if `clock` is on a trading
    day (skip weekends/holidays silently, matching how every other script
    in this repo treats non-trading days), then advance `clock` to the next
    trading day regardless. Split out from the sleep loop below so it can
    be driven directly by a test or by a caller with its own scheduling."""
    d = clock.current.date()
    if calendar.is_trading_day(d):
        result = pipeline.run_session(clock.now())
        app.state.record_session(result)
        _print_session_result(d, result)
        pipeline.execution.broker.settle()
    clock.current = datetime.combine(calendar.next_trading_day(d), time(15, 0),
                                     tzinfo=timezone.utc)


def _run_forever(pipeline: TradingPipeline, calendar: TradingCalendar, clock: FrozenClock,
                 app, interval_seconds: float, lock: threading.Lock,
                 stop_event: threading.Event) -> None:
    """Runs in a background thread with no one watching its return value —
    an unhandled exception here (a transient API timeout, a rate limit)
    would silently kill this thread while uvicorn keeps serving in the main
    thread, leaving a dashboard that LOOKS alive but has permanently
    stopped advancing, with nothing but a traceback in the terminal to
    explain why. Every iteration is caught and logged instead, so a
    transient failure costs one missed session, not the rest of the run."""
    while not stop_event.is_set():
        try:
            with lock:
                advance_one_session(pipeline, calendar, clock, app)
        except Exception as e:  # noqa: BLE001 — must never kill this loop
            print(f"\n!!! session error ({clock.current.date().isoformat()}): "
                 f"{type(e).__name__}: {e}\n    will retry at the next interval\n")
        stop_event.wait(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval", type=float, default=30.0,
                        help="real seconds between simulated trading sessions")
    parser.add_argument("--cash", type=float, default=670.0)
    parser.add_argument("--start", type=str, default=None,
                        help="YYYY-MM-DD first session date (default: today)")
    parser.add_argument("--llm", choices=("mock", "real"), default="mock",
                        help="mock (default, free, deterministic) or real "
                             "(actual Claude/OpenAI API calls, requires credentials)")
    args = parser.parse_args()

    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))

    calendar = TradingCalendar()
    start_date = date.fromisoformat(args.start) if args.start else datetime.now(timezone.utc).date()
    if not calendar.is_trading_day(start_date):
        start_date = calendar.next_trading_day(start_date - timedelta(days=1))
    clock = FrozenClock(current=datetime.combine(start_date, time(15, 0), tzinfo=timezone.utc))

    pipeline = _build_pipeline(args.cash, universe, clock, use_real_llm=args.llm == "real")

    # docs/SAFETY_AUDIT.md F4 / §21: reconcile against the broker before
    # resuming trading, same as run_dashboard.py.
    startup_recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                         risk_controller=pipeline.risk_controller)
    report = startup_recon.reconcile()
    print("startup reconciliation:", "CONSISTENT" if report.consistent else "MISMATCH")

    # Shared with the API app: TradingPipeline/Ledger have no internal
    # locking, and this script is the one place in the repo that mutates
    # the pipeline from a different thread than the one serving it.
    lock = threading.Lock()
    app = create_app(pipeline, lock=lock)

    stop_event = threading.Event()
    session_thread = threading.Thread(
        target=_run_forever,
        args=(pipeline, calendar, clock, app, args.interval, lock, stop_event),
        daemon=True)
    session_thread.start()

    print(f"\nPAPER trading — environment={pipeline.environment.value} | "
          f"dashboard at http://localhost:{args.port}/")
    print(f"new simulated session every {args.interval:.0f}s, starting "
          f"{start_date.isoformat()} — Ctrl+C to stop\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
