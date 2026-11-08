"""Summarise recorded paper sessions; compare runs side by side.

  python3 scripts/report_performance.py var/runner/sessions.db [other.db ...] [--daily]

Read-only. Prints per-run totals, drawdown and counts, always with the number of sessions.
Few sessions say nothing about profitability, and the report says so."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.common.durable_store import DurableSessionStore
from packages.common.environment import Environment
from services.supervisor.performance_report import render, summarize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", nargs="+")
    parser.add_argument("--daily", action="store_true", help="also list every session")
    args = parser.parse_args(argv)
    summaries = []
    for path in args.db:
        if not Path(path).exists():
            print(f"{path}: no such file", file=sys.stderr)
            return 2
        records = DurableSessionStore(path, Environment.PAPER).all()
        if not records:
            print(f"{path}: no sessions recorded", file=sys.stderr)
            return 2
        summaries.append(summarize(records))
        if args.daily:
            print(f"-- {path}")
            for r in records:
                print(f"  {r.session_date}  equity {r.equity:>9.2f}  day {r.day_pnl:+8.2f}"
                      f"  dd {r.drawdown:6.2%}  filled {r.orders_filled}  {r.risk_state}"
                      f"{'  warnings ' + str(r.data_warnings) if r.data_warnings else ''}")
    print(render(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
