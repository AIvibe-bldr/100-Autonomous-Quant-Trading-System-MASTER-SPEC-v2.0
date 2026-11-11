"""Connectivity check for SEC EDGAR (no LLM, read-only).

  QUANT_SEC_USER_AGENT="YourApp you@your-domain.com" python3 scripts/check_edgar.py [TICKER ...]

SEC requires a User-Agent naming the app and a real contact email; set it in
the environment (it is never defaulted or stored in the repo). Prints the
recent 8-K filings and the estimated next earnings date per ticker, so the
response shapes the code assumes can be confirmed against the live service."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
from services.market_data.edgar_earnings import EdgarEarningsEstimator
from services.news.edgar_insider import parse_form4


def main(argv: list[str]) -> int:
    tickers = argv or ["AAPL", "PFE", "JPM"]
    try:
        client = EdgarClient.from_env()
        today = date.today()
        for t in tickers:
            eights = [f for f in client.recent_filings(t) if f.form == "8-K"][:3]
            print(f"{t}: {len(client.recent_filings(t))} recent filings; latest 8-Ks:")
            for f in eights:
                print(f"   {f.filing_date} items={','.join(f.items) or '-'}  {f.url}")
            for f in [f for f in client.recent_filings(t) if f.form == "4"][:3]:
                s = parse_form4(client.document_text(f))
                print(f"   Form 4 {f.filing_date}:", s if s and s.has_open_market_trade else "no open-market trade")
            est = EdgarEarningsEstimator(client).fetch([t], today, today + timedelta(days=100))
            print("   estimated earnings:", [(e.on.isoformat(), e.uncertainty_days) for e in est] or "none")
    except EdgarUnavailable as e:
        print(f"EDGAR unavailable: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
