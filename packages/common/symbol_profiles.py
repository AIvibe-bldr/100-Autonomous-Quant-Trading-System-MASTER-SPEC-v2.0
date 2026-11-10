"""Display reference for the dashboard: who a ticker is, in plain Japanese.

Per ticker: the Japanese company name, the English name, the category ("何系"), and one line on what the company
does. Pure data, for people reading the screen. Nothing in trading reads it (themes used for exposure limits are
`services/composition.DEFAULT_THEMES`). A ticker that is not listed here is shown by its symbol alone — a name is
never guessed."""
from __future__ import annotations

from typing import NamedTuple, Optional


class SymbolProfile(NamedTuple):
    company_ja: str
    company: str
    category: str
    business: str


SYMBOL_PROFILES: dict[str, SymbolProfile] = {
    "AAPL": SymbolProfile("アップル", "Apple", "消費者向けテック", "iPhone・Mac・Apple Watch などを作る"),
    "MSFT": SymbolProfile("マイクロソフト", "Microsoft", "AI・ソフトウェア", "Windows・Office・クラウド（Azure）・AI"),
    "NVDA": SymbolProfile("エヌビディア", "NVIDIA", "AI・半導体", "AI の計算に使う半導体（GPU）の最大手"),
    "AMD": SymbolProfile("AMD（アドバンスト・マイクロ・デバイセズ）", "AMD", "AI・半導体", "パソコン・サーバー・AI 向けの半導体"),
    "AVGO": SymbolProfile("ブロードコム", "Broadcom", "AI・半導体", "通信・データセンター向けの半導体とソフトウェア"),
    "TSLA": SymbolProfile("テスラ", "Tesla", "電気自動車", "電気自動車と蓄電池を作る"),
    "AMZN": SymbolProfile("アマゾン", "Amazon", "ネット通販・クラウド", "ネット通販と、クラウド（AWS）"),
    "GOOG": SymbolProfile("グーグル（アルファベット）", "Alphabet (Google)", "検索・AI", "検索・広告・YouTube・AI"),
    "META": SymbolProfile("メタ・プラットフォームズ", "Meta Platforms", "SNS・AI", "Facebook・Instagram などの SNS と AI"),
    "NFLX": SymbolProfile("ネットフリックス", "Netflix", "動画配信", "映画やドラマの動画配信"),
    "PLTR": SymbolProfile("パランティア", "Palantir", "データ分析・AI", "政府・企業向けのデータ分析ソフト"),
    "CRWD": SymbolProfile("クラウドストライク", "CrowdStrike", "サイバーセキュリティ", "クラウド型のサイバー攻撃対策"),
    "PANW": SymbolProfile("パロアルトネットワークス", "Palo Alto Networks", "サイバーセキュリティ", "企業向けのネットワーク・セキュリティ製品"),
    "SNOW": SymbolProfile("スノーフレイク", "Snowflake", "クラウドデータ", "企業のデータをためて分析するクラウド基盤"),
    "DDOG": SymbolProfile("データドッグ", "Datadog", "クラウド監視", "クラウドのシステムの状態を監視するサービス"),
    "MRNA": SymbolProfile("モデルナ", "Moderna", "バイオ・医薬", "mRNA を使ったワクチン・医薬品"),
    "PFE": SymbolProfile("ファイザー", "Pfizer", "医薬品", "世界的な大手製薬会社（ワクチン・医薬品）"),
    "XOM": SymbolProfile("エクソンモービル", "Exxon Mobil", "エネルギー（石油）", "世界最大級の石油・ガス会社"),
    "CVX": SymbolProfile("シェブロン", "Chevron", "エネルギー（石油）", "米国の大手石油・ガス会社"),
    "JPM": SymbolProfile("JP モルガン・チェース", "JPMorgan Chase", "金融（銀行）", "米国最大の銀行"),
}


def profile_of(symbol: str) -> Optional[SymbolProfile]:
    return SYMBOL_PROFILES.get(symbol)
