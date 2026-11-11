"""Display reference for the dashboard: who a ticker is, in plain Japanese.

Per ticker: the Japanese company name, the English name, the category ("何系"), and one line on what the company
does. Pure data, for people reading the screen. Nothing in trading reads it (themes used for exposure limits are
`services/composition.DEFAULT_THEMES`). A ticker that is not listed here is shown by its symbol alone — a name is
never guessed."""
from __future__ import annotations

from typing import NamedTuple, Optional


class SymbolProfile(NamedTuple):
    company_ja: str            # "" when the profile was fetched automatically (names are not translated)
    company: str
    category: str
    business: str
    auto: bool = False         # True: filled from the SEC's company record, not written by hand


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


# --- automatic profile for a ticker that is not in the table above ---------------------------------------------
# The SEC records one industry code (SIC) per company. It says what KIND of company it is, not what it does, and
# it has no description text — so an automatic profile has a category and an honest note, never a made-up summary.
SIC_CATEGORY_JA: dict[int, str] = {
    7372: "ソフトウェア", 7370: "ITサービス", 7371: "ITサービス", 7373: "ITサービス", 7374: "ITサービス（データ処理）",
    3674: "半導体", 3672: "電子部品", 3661: "通信機器", 3663: "通信機器", 3669: "通信機器", 3571: "コンピュータ",
    3572: "コンピュータ（記憶装置）", 3576: "ネットワーク機器", 3577: "コンピュータ周辺機器", 3570: "コンピュータ",
    3711: "自動車", 3714: "自動車部品", 3721: "航空機", 3760: "ミサイル・宇宙", 3812: "防衛・航法機器",
    2834: "医薬品", 2836: "バイオ医薬", 2835: "診断薬", 2833: "医薬品原料", 3841: "医療機器", 3845: "医療機器（電子）",
    8000: "医療サービス", 8011: "医療サービス", 6324: "医療保険",
    2911: "エネルギー（石油精製）", 1311: "エネルギー（石油・ガス採掘）", 1381: "エネルギー（掘削）", 4911: "電力", 4923: "ガス事業",
    4813: "通信サービス", 4841: "ケーブル・放送", 4899: "通信サービス", 7812: "映画・映像制作", 7822: "映像配給", 7841: "映像配信",
    6021: "銀行", 6022: "銀行", 6029: "銀行", 6199: "金融サービス", 6211: "証券・投資銀行", 6282: "資産運用",
    6311: "生命保険", 6331: "損害保険", 6798: "不動産投資信託（REIT）",
    5331: "小売（総合）", 5311: "小売（百貨店）", 5399: "小売（総合）", 5411: "小売（食品）", 5812: "外食", 5961: "通販",
    5990: "小売（専門）", 5200: "小売（住宅関連）", 5651: "小売（衣料）", 5731: "小売（家電）",
    2080: "飲料", 2086: "飲料", 2000: "食品", 2111: "たばこ", 2844: "化粧品・日用品", 2840: "日用品", 3140: "靴・衣料", 3021: "靴・衣料", 2300: "衣料", 3949: "スポーツ用品",
    4512: "航空", 4400: "海運", 4011: "鉄道", 4213: "トラック輸送", 4731: "物流",
    3490: "金属製品", 3310: "鉄鋼", 1040: "金鉱業", 1000: "鉱業", 2860: "化学", 2820: "化学", 2810: "化学",
    7389: "ビジネスサービス", 7380: "ビジネスサービス", 8742: "経営コンサル", 7990: "娯楽・レジャー", 7011: "ホテル・カジノ",
}
_SIC_DIVISIONS_JA: tuple[tuple[int, int, str], ...] = (
    (100, 999, "農林水産"), (1000, 1499, "鉱業・資源"), (1500, 1799, "建設"), (2000, 3999, "製造業"),
    (4000, 4999, "運輸・通信・公益"), (5000, 5199, "卸売"), (5200, 5999, "小売"), (6000, 6799, "金融・保険・不動産"),
    (7000, 8999, "サービス"), (9100, 9999, "公共・その他"),
)


def category_ja_from_sic(sic: Optional[int]) -> Optional[str]:
    """Japanese category for an SEC industry code: the exact code if known, else its broad division, else None."""
    if sic is None:
        return None
    if sic in SIC_CATEGORY_JA:
        return SIC_CATEGORY_JA[sic]
    for lo, hi, label in _SIC_DIVISIONS_JA:
        if lo <= sic <= hi:
            return label
    return None


def profile_from_industry(company: str, sic: Optional[int], sic_description: str) -> SymbolProfile:
    """Automatic profile from the SEC's record. The company name stays as the SEC spells it (not translated); the
    category is Japanese when the industry code is known, else the SEC's English industry text; the business line
    says plainly that it is an industry class and not a description of what the company does."""
    english = (sic_description or "").strip()
    category = category_ja_from_sic(sic) or english or "業種は不明"
    note = f"SEC の業種分類: {english}" if english else "SEC に業種の記載がありません"
    return SymbolProfile("", company.strip(), category, note + "（自動取得。事業内容の説明ではありません）", True)
