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
from services.cost_manager.engine import CostCategory, CostEntry, OperatingCostEngine
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.pdca.review import DailyReview
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
    costs = OperatingCostEngine()
    print(f"=== PAPER environment | initial cash ${args.cash:,.2f} (§73: 環境表示) ===")

    sessions = 0
    prev_equity = args.cash
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

        # Daily PDCA review (§63-64, §92) + running AI/server cost (§80)
        costs.record(CostEntry(at=at, category=CostCategory.AI, amount=0.50,
                               note="decision/skeptic model calls"))
        costs.record(CostEntry(at=at, category=CostCategory.SERVER, amount=0.10))
        review = DailyReview(day=d, pnl=snap.equity - prev_equity, equity=snap.equity,
                             start_equity=args.cash, day_index=sessions + 1, result=result,
                             data_issues=len(pipeline.integrity.issues))
        grades = review.compute_grades()
        print(f"review: overall {grades['overall']} "
              f"(risk {grades['risk_compliance']}, data {grades['data_quality']}, "
              f"pnl {grades['pnl']})")
        for line in review.narrative():
            print(f"  {line}")
        prev_equity = snap.equity
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

    # Two P&L (§81): trading result and project net result shown separately
    prices = {s: pipeline.market_data.quote(s, clock.now()).mid
              for s in pipeline.ledger.positions}
    trading_pnl = pipeline.ledger.snapshot(prices).equity - args.cash
    two = costs.two_pnl(trading_pnl)
    print(f"\nTrading P&L:     ${two.trading_pnl:+,.2f}")
    print(f"Operating Costs: ${two.operating_costs:,.2f} "
          f"({', '.join(f'{k.value}: {v:.2f}' for k, v in costs.by_category().items())})")
    print(f"Project Net P&L: ${two.project_net_pnl:+,.2f} (§81)")


if __name__ == "__main__":
    main()
