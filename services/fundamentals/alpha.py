"""Fundamental Inflection as an Alpha Factory candidate (research-instruction
§12 "Fundamental Alphaと既存Alphaの統合" / §21 "Fundamental AlphaのLIVE昇格に
Promotion Gateが必要であること").

`FundamentalInflectionAlpha` implements `services.alpha_factory.factory.
Alpha`'s Protocol exactly (`name`, `version`, `signal(bars) -> list[bool]`),
so it runs through `AlphaJudge`/`ChampionChallenger` completely unmodified —
the same backtest -> walk-forward -> Monte Carlo -> shadow -> promotion-gate
ladder every other alpha goes through, never a special-cased fast path
(§11's own warning against hardcoding a "confirmed" screen).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from packages.schemas.core import Bar
from services.fundamentals.engine import FundamentalInflectionEngine

ALPHA_FAMILY = "FUNDAMENTAL_INFLECTION"


@dataclass
class FundamentalInflectionAlpha:
    """entry_signal[i] = whether STRUCTURAL_IMPROVEMENT held as of bar i's
    own close — each call is independently Point-in-Time correct
    (FundamentalInflectionEngine.analyze() filters by `received_timestamp`),
    so this has no lookahead: bar i's signal cannot depend on a quarter
    filed after bar i's own date, exactly like MomentumAlpha/
    MeanReversionAlpha's own "decided on close of bar i" contract."""

    engine: FundamentalInflectionEngine = field(default_factory=FundamentalInflectionEngine)
    name: str = ALPHA_FAMILY.lower()
    version: str = "1.0.0"

    def signal(self, bars: list[Bar]) -> list[bool]:
        return [self.engine.analyze(bar.symbol, bar.ts).actionable for bar in bars]
