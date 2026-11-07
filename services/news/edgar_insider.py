"""Insider open-market trades (SEC Form 4) as SEC_PRIMARY news (MASTER SPEC §18).

Only STRUCTURED fields of the Form 4 XML are read — transaction code, shares,
price, acquired/disposed flag and the owner-relationship booleans — and the text
handed on is rendered from our own template. Filer-written text (owner name,
officer title, footnotes, free-text security titles) is never read or passed on,
so this source cannot carry injected content (§19).

Only open-market purchases (code P) and sales (code S) of non-derivative
securities are reported: awards, option exercises, tax withholding, gifts and
the like say little about conviction and are dropped. The XML is size-capped and
refused if it declares a DOCTYPE/ENTITY (no entity-expansion tricks)."""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from services.market_data.edgar_client import EdgarClient, EdgarUnavailable, Filing
from services.news.engine import NewsItem, SourceTier
from services.news.source import NewsUnavailable

MAX_FILINGS_PER_SYMBOL = 8           # bounds SEC requests per symbol per fetch
_MAX_SHARES = 1e10
_MAX_PRICE = 1e6


@dataclass(frozen=True)
class InsiderSummary:
    buy_shares: float
    buy_value: float
    sell_shares: float
    sell_value: float
    roles: tuple[str, ...]            # subset of director / officer / 10%-owner
    planned_10b5_1: bool

    @property
    def has_open_market_trade(self) -> bool:
        return self.buy_shares > 0 or self.sell_shares > 0


def _text(node: Optional[ET.Element], path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def _positive(raw: str, cap: float) -> Optional[float]:
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if math.isfinite(v) and 0 < v <= cap else None


def parse_form4(xml_text: str) -> Optional[InsiderSummary]:
    """Summary of the open-market trades in one Form 4, or None when the document
    is unreadable or holds no open-market purchase/sale."""
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if _text(root, "documentType") != "4":
        return None
    buy_sh = buy_val = sell_sh = sell_val = 0.0
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode")
        flag = _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value")
        shares = _positive(_text(tx, "transactionAmounts/transactionShares/value"), _MAX_SHARES)
        price = _positive(_text(tx, "transactionAmounts/transactionPricePerShare/value"), _MAX_PRICE)
        if shares is None or price is None:
            continue                                  # unknown price/size: not reported as a fact
        if code == "P" and flag == "A":
            buy_sh, buy_val = buy_sh + shares, buy_val + shares * price
        elif code == "S" and flag == "D":
            sell_sh, sell_val = sell_sh + shares, sell_val + shares * price
    roles = []
    rel = root.findall("reportingOwner/reportingOwnerRelationship")
    for tag, name in (("isDirector", "director"), ("isOfficer", "officer"),
                      ("isTenPercentOwner", "10%-owner")):
        if any(_text(r, tag).lower() in ("1", "true") for r in rel):
            roles.append(name)
    return InsiderSummary(buy_sh, buy_val, sell_sh, sell_val, tuple(roles),
                          _text(root, "aff10b5One").lower() in ("1", "true"))


def _describe(s: InsiderSummary) -> str:
    parts = []
    if s.buy_shares:
        parts.append(f"open-market PURCHASE {s.buy_shares:,.0f} sh, avg ${s.buy_value / s.buy_shares:,.2f} "
                     f"(~${s.buy_value:,.0f})")
    if s.sell_shares:
        parts.append(f"open-market SALE {s.sell_shares:,.0f} sh, avg ${s.sell_value / s.sell_shares:,.2f} "
                     f"(~${s.sell_value:,.0f})")
    who = "/".join(s.roles) or "role not stated"
    plan = "under a Rule 10b5-1 plan" if s.planned_10b5_1 else "not flagged as a 10b5-1 plan"
    return f"Form 4 insider ({who}): " + "; ".join(parts) + f"; {plan}"


class EdgarInsiderNewsSource:
    def __init__(self, client: EdgarClient) -> None:
        self._client = client

    def fetch(self, symbols: list[str], since: datetime, until: datetime) -> list[NewsItem]:
        items: list[NewsItem] = []
        try:
            for sym in symbols:
                in_window: list[Filing] = [
                    f for f in self._client.recent_filings(sym)
                    if f.form == "4" and since <= f.accepted_at <= until]       # no lookahead
                for f in in_window[:MAX_FILINGS_PER_SYMBOL]:
                    summary = parse_form4(self._client.document_text(f))
                    if summary is None or not summary.has_open_market_trade:
                        continue
                    kind = ("BUY" if summary.buy_shares and not summary.sell_shares else
                            "SELL" if summary.sell_shares and not summary.buy_shares else "BUY+SELL")
                    items.append(NewsItem(
                        title=f"{sym} insider open-market {kind} (Form 4)",
                        text=_describe(summary), url=f.url, source="sec-edgar-form4",
                        tier=SourceTier.SEC_PRIMARY, published_at=f.accepted_at, tickers=(sym,)))
        except EdgarUnavailable as e:
            raise NewsUnavailable(str(e)) from None
        return items
