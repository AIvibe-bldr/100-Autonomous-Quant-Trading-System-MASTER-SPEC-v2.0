"""Ask the independent Alpha Judge for recommendations over daily history (read-only research).

  python3 scripts/judge_alphas.py [--days 500] [--real-data] [SYMBOL ...]

Prints, per alpha and symbol, how far it got through backtest -> walk-forward -> luck test and what the
Judge recommends. A recommendation is NOT a promotion: nothing here changes what the system trades.
With few trades the answer is 'research more' — that is the honest answer for short histories."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.alpha_factory.report import judge_alphas
from services.composition import DEFAULT_SYMBOLS
from services.market_data.service import MarketDataService, MarketDataUnavailable, MockProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("symbols", nargs="*")
    parser.add_argument("--days", type=int, default=500)
    parser.add_argument("--real-data", action="store_true", help="Alpaca daily bars (needs APCA_* keys)")
    args = parser.parse_args(argv)

    if args.real_data:
        from services.market_data.alpaca_provider import AlpacaMarketDataProvider
        try:
            provider = AlpacaMarketDataProvider.from_env()
        except MarketDataUnavailable as e:
            print(f"--real-data: {e}", file=sys.stderr)
            return 2
    else:
        provider = MockProvider()
    md = MarketDataService(provider)
    now = datetime.now(timezone.utc)
    symbols = args.symbols or DEFAULT_SYMBOLS[:5]
    try:
        bars = {s: [b.bar for b in md.bars(s, now, args.days, now)] for s in symbols}
    except MarketDataUnavailable as e:
        print(f"market data unavailable: {e}", file=sys.stderr)
        return 1
    print(f"{'alpha':15} {'symbol':6} {'bars':>5} {'trades':>6} {'return':>8} {'oos':>8} {'p':>5}  recommendation")
    for r in judge_alphas(bars):
        ret = "n/a" if r["total_return"] is None else f"{r['total_return']:+.1%}"
        print(f"{r['alpha']:15} {r['symbol']:6} {r['bars']:5d} {r['trades']:6d} {ret:>8} "
              f"{r['oos_return']:+8.1%} {r['p_value']:5.2f}  {r['recommendation']}"
              + (f"  ({r['reasons'][0]})" if r["reasons"] else ""))
    print("Recommendations only: nothing is promoted by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
