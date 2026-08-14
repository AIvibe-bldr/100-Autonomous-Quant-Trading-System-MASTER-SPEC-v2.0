"""Paper trading demo — one full session end-to-end (MASTER SPEC §107).

Usage:  python3 scripts/run_paper_demo.py [--days N]

Runs N consecutive paper sessions on the deterministic mock market and prints
the funnel, trades, NO TRADE reasons (§93), equity/HWM/drawdown (§5) and the
audit-completeness score (§75).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from tests.conftest import SYMBOLS, build_pipeline
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.reconciliation.engine import ReconciliationEngine


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--cash", type=float, default=670.0, help="≈ ¥100,000 in USD")
    args = parser.parse_args()

    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))

    cal = TradingCalendar()
    d = date(2026, 8, 10)
    clock = FrozenClock(current=datetime.combine(d, time(15, 0), tzinfo=timezone.utc))
    pipeline = build_pipeline(clock, universe, initial_cash=args.cash)
    print(f"=== PAPER environment | initial cash ${args.cash:,.2f} (§73: 環境表示) ===")

    sessions = 0
    while sessions < args.days:
        if not cal.is_trading_day(d):
            d = d + timedelta(days=1)
            continue
        at = datetime.combine(d, time(15, 0), tzinfo=timezone.utc)
        clock.current = at
        result = pipeline.run_session(at)
        broker = pipeline.execution.broker
        broker.settle()  # T+1 tick between sessions (§14)

        prices = {s: pipeline.market_data.quote(s, at).mid for s in pipeline.ledger.positions}
        snap = pipeline.ledger.snapshot(prices)
        print(f"\n--- {d} ---")
        print(f"funnel: scanned {result.scanned} → candidates {result.candidates} → "
              f"decisions {result.decision_candidates} → sized {result.sized} → "
              f"risk passed {result.risk_passed} → filled {result.orders_filled}")
        for f in result.fills:
            print(f"  FILL {f.side.value} {f.qty:g} {f.symbol} @ {f.price:.2f} "
                  f"(fees {f.fees:.4f})")
        if not result.traded:
            print("  NO TRADE — reasons (§93):")
            for r in result.no_trade_reasons[:5]:
                print(f"    · {r}")
        print(f"equity ${snap.equity:,.2f} | cash ${snap.cash:,.2f} | "
              f"HWM ${snap.high_water_mark:,.2f} | DD {snap.drawdown:.1%}")
        sessions += 1
        d = d + timedelta(days=1)

    recon = ReconciliationEngine(broker=pipeline.execution.broker, ledger=pipeline.ledger,
                                 risk_controller=pipeline.risk_controller, cash_tolerance=1.0)
    report = recon.reconcile()
    print(f"\nreconciliation: {'CONSISTENT' if report.consistent else 'MISMATCH'}")
    for m in report.mismatches:
        print(f"  ! {m.kind}: {m.detail}")
    print(f"audit completeness: {pipeline.provenance.audit_completeness():.0%} (§75)")
    print(f"risk state: {pipeline.risk_controller.state.value}")


if __name__ == "__main__":
    main()
