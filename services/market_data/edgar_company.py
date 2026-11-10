"""Automatic company profile for a ticker that the hand-written table does not cover (display only).

Name and industry class come from the SEC's own company record; nothing is translated, summarised or guessed."""
from __future__ import annotations

from typing import Optional

from packages.common.symbol_profiles import SymbolProfile, profile_from_industry
from services.market_data.edgar_client import EdgarClient


def edgar_company_profile(client: EdgarClient, symbol: str) -> Optional[SymbolProfile]:
    info = client.company_info(symbol)
    if info is None or not info.name:
        return None
    return profile_from_industry(info.name, info.sic, info.sic_description)
