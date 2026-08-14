"""Run the status API + dashboard against a demo paper session.

Usage:  python3 scripts/run_dashboard.py [--port 8000] [--days 5]

Replays N paper sessions to populate state, then serves the read-only
dashboard (§86-97) at http://localhost:<port>/.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from apps.api.main import create_app
from packages.common.clock import FrozenClock
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.quant.replay import ReplayEngine
from tests.conftest import SYMBOLS, build_pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--cash", type=float, default=670.0)
    args = parser.parse_args()

    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0),
                                                 tzinfo=timezone.utc))
    pipeline = build_pipeline(clock, universe, initial_cash=args.cash)
    app = create_app(pipeline)

    report = ReplayEngine(pipeline).run(start=date(2026, 8, 3), sessions=args.days)
    for day in report.days:
        app.state.record_session(day.result)
    print(f"replayed {report.total_sessions} sessions; final equity "
          f"${report.final_equity:,.2f}; dashboard at http://localhost:{args.port}/")

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
