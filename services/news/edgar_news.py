"""SEC 8-K filings as SEC_PRIMARY news (MASTER SPEC §18 tier 1).

Only what the SEC index states is used: form type, acceptance time and the
8-K item numbers, rendered through OUR table of item names. No filer-written
text is fetched or passed on, so this source cannot carry injected content."""
from __future__ import annotations

from datetime import datetime

from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
from services.news.engine import NewsItem, SourceTier
from services.news.source import NewsUnavailable

ITEM_NAMES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Election of Directors or Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


class EdgarFilingsNewsSource:
    def __init__(self, client: EdgarClient) -> None:
        self._client = client

    def fetch(self, symbols: list[str], since: datetime, until: datetime) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            for sym in symbols:
                for f in self._client.recent_filings(sym):
                    if f.form != "8-K" or not (since <= f.accepted_at <= until):
                        continue                              # no lookahead, 8-K only
                    names = [f"{i} {ITEM_NAMES.get(i, 'other')}" for i in f.items] or ["no items listed"]
                    items.append(NewsItem(
                        title=f"{sym} filed Form 8-K (Items: {', '.join(f.items) or '-'})",
                        text="Form 8-K items: " + "; ".join(names),
                        url=f.url, source="sec-edgar", tier=SourceTier.SEC_PRIMARY,
                        published_at=f.accepted_at, tickers=(sym,)))
        except EdgarUnavailable as e:
            raise NewsUnavailable(str(e)) from None
        return items
