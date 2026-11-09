"""Display reference for the dashboard: a company name and a plain-language category per ticker.

Pure data, for people reading the screen. Nothing in trading reads it (themes used for exposure limits are
`services/composition.DEFAULT_THEMES`). A ticker that is not listed here is shown by its symbol alone — a name
is never guessed."""
from __future__ import annotations

from typing import NamedTuple, Optional


class SymbolProfile(NamedTuple):
    company: str
    category: str


SYMBOL_PROFILES: dict[str, SymbolProfile] = {
    "AAPL": SymbolProfile("Apple", "消費者向けテック"),
    "MSFT": SymbolProfile("Microsoft", "AI・ソフトウェア"),
    "NVDA": SymbolProfile("NVIDIA", "AI・半導体"),
    "AMD": SymbolProfile("AMD", "AI・半導体"),
    "AVGO": SymbolProfile("Broadcom", "AI・半導体"),
    "TSLA": SymbolProfile("Tesla", "電気自動車"),
    "AMZN": SymbolProfile("Amazon", "ネット通販・クラウド"),
    "GOOG": SymbolProfile("Alphabet (Google)", "検索・AI"),
    "META": SymbolProfile("Meta Platforms", "SNS・AI"),
    "NFLX": SymbolProfile("Netflix", "動画配信"),
    "PLTR": SymbolProfile("Palantir", "データ分析・AI"),
    "CRWD": SymbolProfile("CrowdStrike", "サイバーセキュリティ"),
    "PANW": SymbolProfile("Palo Alto Networks", "サイバーセキュリティ"),
    "SNOW": SymbolProfile("Snowflake", "クラウドデータ"),
    "DDOG": SymbolProfile("Datadog", "クラウド監視"),
    "MRNA": SymbolProfile("Moderna", "バイオ・医薬"),
    "PFE": SymbolProfile("Pfizer", "医薬品"),
    "XOM": SymbolProfile("Exxon Mobil", "エネルギー（石油）"),
    "CVX": SymbolProfile("Chevron", "エネルギー（石油）"),
    "JPM": SymbolProfile("JPMorgan Chase", "金融（銀行）"),
}


def profile_of(symbol: str) -> Optional[SymbolProfile]:
    return SYMBOL_PROFILES.get(symbol)
