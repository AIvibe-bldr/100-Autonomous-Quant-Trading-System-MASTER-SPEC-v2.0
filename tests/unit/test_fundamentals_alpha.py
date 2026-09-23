"""Tests for services/fundamentals/alpha.py (research-instruction §12/§21).

§21's own required test this file targets:
- "Fundamental AlphaのLIVE昇格にPromotion Gateが必要であること" — proven by
  running FundamentalInflectionAlpha through the SAME, unmodified
  AlphaJudge/ChampionChallenger every other alpha goes through (never a
  special-cased path straight to PROMOTED).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from packages.schemas.core import Bar
from services.alpha_factory.factory import AlphaJudge, ChampionChallenger, JudgeRecommendation
from services.fundamentals.alpha import ALPHA_FAMILY, FundamentalInflectionAlpha
from services.fundamentals.engine import FundamentalInflectionEngine
from services.fundamentals.mock_source import _seed

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _daily_bars(symbol: str, days: int) -> list[Bar]:
    bars = []
    price = 100.0
    for i in range(days):
        ts = START + timedelta(days=i)
        bars.append(Bar(symbol=symbol, ts=ts, open=price, high=price * 1.01,
                        low=price * 0.99, close=price, volume=1_000_000))
        price *= 1.0002   # slow, irrelevant-to-the-signal drift
    return bars


def test_satisfies_the_alpha_protocol():
    alpha = FundamentalInflectionAlpha()
    assert alpha.name == ALPHA_FAMILY.lower()
    assert isinstance(alpha.version, str) and alpha.version
    bars = _daily_bars("AAPL", 5)
    signal = alpha.signal(bars)
    assert len(signal) == len(bars)
    assert all(isinstance(s, bool) for s in signal)


def test_signal_matches_the_underlying_engines_actionable_flag():
    alpha = FundamentalInflectionAlpha()
    bars = _daily_bars("AAPL", 10)
    signal = alpha.signal(bars)
    engine = FundamentalInflectionEngine()
    expected = [engine.analyze(b.symbol, b.ts).actionable for b in bars]
    assert signal == expected


def test_a_structurally_improving_archetype_signals_true_somewhere():
    symbol = next(f"ALPHAPROBE{i}" for i in range(200)
                 if _seed(f"ALPHAPROBE{i}", "archetype") % 6 == 0)
    alpha = FundamentalInflectionAlpha()
    bars = _daily_bars(symbol, 400)   # >1 year: enough quarters to accumulate filings
    signal = alpha.signal(bars)
    assert any(signal), "a structurally-improving archetype never signaled True"


def test_a_deteriorating_archetype_never_signals_true():
    symbol = next(f"ALPHAPROBE{i}" for i in range(200)
                 if _seed(f"ALPHAPROBE{i}", "archetype") % 6 == 1)
    alpha = FundamentalInflectionAlpha()
    bars = _daily_bars(symbol, 400)
    signal = alpha.signal(bars)
    assert not any(signal), "a deteriorating archetype signaled True somewhere"


def test_runs_through_the_unmodified_alpha_judge_without_a_special_case():
    """§21: never a fast path to PROMOTED — the Judge's strongest possible
    output stays PROMOTE_RECOMMENDED (a recommendation), and running this
    alpha through it must not require touching AlphaJudge itself at all."""
    symbol = next(f"ALPHAPROBE{i}" for i in range(200)
                 if _seed(f"ALPHAPROBE{i}", "archetype") % 6 == 0)
    alpha = FundamentalInflectionAlpha()
    bars = _daily_bars(symbol, 400)
    verdict = AlphaJudge().judge(alpha, bars)
    assert verdict.alpha == alpha.name
    assert verdict.recommendation is not None
    # the Judge can NEVER itself set PROMOTED — only a recommendation
    from services.alpha_factory.factory import AlphaStage
    assert verdict.stage is not AlphaStage.PROMOTED


def test_promotion_still_requires_shadow_sessions_via_champion_challenger():
    """§21: LIVE promotion needs the Promotion Gate — a single good
    judge verdict is not itself a promotion."""
    cc = ChampionChallenger(min_shadow_sessions=10)
    promoted, reason = cc.consider_promotion(ALPHA_FAMILY)
    assert not promoted
    assert "shadow" in reason.lower()
