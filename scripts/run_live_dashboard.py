"""Continuously-running PAPER dashboard.

scripts/run_dashboard.py replays a fixed number of PAST days once and then
serves a static dashboard forever. This script instead keeps ONE
TradingPipeline alive for the life of the process and advances it one
trading-calendar session at a time on a fixed real-world interval, while
serving the dashboard the whole time — so a user can leave this running and
watch results accumulate in real time, instead of re-running a script.

State (ledger, decision-quality snapshots, the equity-chart series) is
checkpointed to a SQLite file after every session via
packages.common.durable_pipeline_state.DurablePipelineState — stopping the
process and re-running this same command resumes from the last completed
session instead of starting over. Delete --db (or point it at a new path)
to start a fresh PAPER run from --cash/--start instead. This is narrowly
scoped persistence for this one script, not the full 15-table V1 schema
docs/database.md describes — that remains separate, future work.

Environment is always PAPER (packages.common.environment) — no real broker
adapter exists in this repo (packages/broker_adapters has PaperBroker only),
so no real money is ever at risk here regardless of flags.

Usage:
    python3 scripts/run_live_dashboard.py [--port 8000] [--interval 30]
                                          [--cash 670] [--start 2026-08-10]
                                          [--db run_live_dashboard.db]

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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from apps.api.main import create_app
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.durable_pipeline_state import DurablePipelineState
from packages.common.environment import Environment
from packages.common.llm_client import DEFAULT_MODEL_CONFIG, credentials_available
from packages.schemas.core import BrokerPosition
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


def _restore_pipeline_state(pipeline: TradingPipeline, store: DurablePipelineState,
                            clock: FrozenClock) -> None:
    """Overwrites a freshly-built pipeline's ledger/decision-quality/broker
    state with what was checkpointed before the last restart. Must run
    BEFORE the startup ReconciliationEngine call below — PaperBroker tracks
    its own independent cash/positions (it stands in for a real broker), so
    an unseeded broker would show empty holdings against a ledger full of
    real positions and reconciliation would halt new entries on every
    restart (see PaperBroker.seed_state's docstring)."""
    pipeline.ledger = store.load_ledger()
    for snap in store.load_decision_snapshots():
        pipeline.decision_quality.record(snap)
    # _evals aren't persisted (they're fully re-derivable from snapshots +
    # deterministic Mock prices) — re-grade whatever horizons already
    # matured before the restart.
    pipeline.decision_quality.track(
        clock.now(), price_fn=lambda s, t: pipeline.market_data.quote(s, t).mid)
    # advance_one_session() always calls broker.settle() at the end of a
    # session, and every save() happens right after that — so at the point
    # any checkpoint was written, the ledger's cash was already fully
    # settled and unsettled_cash is always 0 here.
    pipeline.execution.broker.seed_state(
        settled_cash=pipeline.ledger.cash, unsettled_cash=0.0,
        positions={s: BrokerPosition(symbol=s, qty=lot.qty, avg_cost=lot.avg_cost)
                  for s, lot in pipeline.ledger.positions.items()})


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
                        clock: FrozenClock, app,
                        store: Optional[DurablePipelineState] = None) -> None:
    """One simulated trading day: run a session if `clock` is on a trading
    day (skip weekends/holidays silently, matching how every other script
    in this repo treats non-trading days), then advance `clock` to the next
    trading day regardless. Split out from the sleep loop below so it can
    be driven directly by a test or by a caller with its own scheduling.

    `store` is optional (defaults to no persistence) so existing callers/
    tests that only care about session-advancing behaviour don't need a
    real DurablePipelineState — scripts/run_live_dashboard.py's own main()
    always passes one."""
    d = clock.current.date()
    if calendar.is_trading_day(d):
        session_at = clock.now()
        result = pipeline.run_session(session_at)
        app.state.record_session(result)
        _print_session_result(d, result)
        pipeline.execution.broker.settle()
        pipeline.decision_quality.track(
            session_at, price_fn=lambda s, t: pipeline.market_data.quote(s, t).mid)
        if store is not None:
            store.save(pipeline.ledger, pipeline.decision_quality,
                      app.state.equity_series, last_session_at=session_at)
    clock.current = datetime.combine(calendar.next_trading_day(d), time(15, 0),
                                     tzinfo=timezone.utc)


def _run_forever(pipeline: TradingPipeline, calendar: TradingCalendar, clock: FrozenClock,
                 app, interval_seconds: float, lock: threading.Lock,
                 stop_event: threading.Event,
                 store: Optional[DurablePipelineState] = None) -> None:
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
                advance_one_session(pipeline, calendar, clock, app, store)
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
    parser.add_argument("--cash", type=float, default=670.0,
                        help="starting cash for a FRESH run — ignored when --db already "
                             "holds saved state (the saved cash/positions are restored)")
    parser.add_argument("--start", type=str, default=None,
                        help="YYYY-MM-DD first session date (default: today) — ignored "
                             "when --db already holds saved state (resumes the day after "
                             "the last saved session)")
    parser.add_argument("--db", type=str, default="run_live_dashboard.db",
                        help="SQLite file for durable state (default: %(default)s in the "
                             "current directory). Delete it, or pass a new path, to start "
                             "a fresh PAPER run from --cash/--start.")
    parser.add_argument("--llm", choices=("mock", "real"), default="mock",
                        help="mock (default, free, deterministic) or real "
                             "(actual Claude/OpenAI API calls, requires credentials)")
    args = parser.parse_args()

    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))

    calendar = TradingCalendar()
    store = DurablePipelineState(args.db, Environment.PAPER)
    resuming = store.has_saved_state()

    if resuming:
        start_date = calendar.next_trading_day(store.load_last_session_at().date())
    else:
        start_date = date.fromisoformat(args.start) if args.start else datetime.now(timezone.utc).date()
        if not calendar.is_trading_day(start_date):
            start_date = calendar.next_trading_day(start_date - timedelta(days=1))
    clock = FrozenClock(current=datetime.combine(start_date, time(15, 0), tzinfo=timezone.utc))

    pipeline = _build_pipeline(args.cash, universe, clock, use_real_llm=args.llm == "real")

    equity_series: list = []
    if resuming:
        _restore_pipeline_state(pipeline, store, clock)
        equity_series = store.load_equity_series()
        print(f"resumed from {args.db}: {len(pipeline.ledger.entries)} ledger entries, "
              f"{len(equity_series)} equity points, cash ${pipeline.ledger.cash:,.2f}, "
              f"next session {start_date.isoformat()}")
    else:
        print(f"no saved state at {args.db} — starting a fresh PAPER run from ${args.cash:,.2f}")

    # docs/SAFETY_AUDIT.md F4 / §21: reconcile against the broker before
    # resuming trading, same as run_dashboard.py. Also the load-bearing check
    # that a restore's seed_state() above actually matches the ledger.
    startup_recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                         risk_controller=pipeline.risk_controller)
    report = startup_recon.reconcile()
    print("startup reconciliation:", "CONSISTENT" if report.consistent else "MISMATCH")

    # Shared with the API app: TradingPipeline/Ledger have no internal
    # locking, and this script is the one place in the repo that mutates
    # the pipeline from a different thread than the one serving it.
    lock = threading.Lock()
    app = create_app(pipeline, lock=lock, initial_equity_series=equity_series)

    stop_event = threading.Event()
    session_thread = threading.Thread(
        target=_run_forever,
        args=(pipeline, calendar, clock, app, args.interval, lock, stop_event, store),
        daemon=True)
    session_thread.start()

    print(f"\nPAPER trading — environment={pipeline.environment.value} | "
          f"dashboard at http://localhost:{args.port}/")
    print(f"new simulated session every {args.interval:.0f}s, starting "
          f"{start_date.isoformat()} — state saved to {args.db} — Ctrl+C to stop\n")
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
