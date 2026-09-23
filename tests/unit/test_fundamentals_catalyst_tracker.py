"""Tests for services/fundamentals/catalyst_tracker.py (research-instruction §9)."""
from __future__ import annotations

from datetime import datetime, timezone

from services.fundamentals.catalyst_tracker import CatalystType, GrowthCatalystTracker
from services.news.engine import NewsSignal, SourceTier

AT = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _signal(headline: str, sns_only: bool = False, injection_flagged: bool = False) -> NewsSignal:
    return NewsSignal(cluster_id="AAPL:2026-08-11", tickers=("AAPL",), headline=headline,
                      tier=SourceTier.MAJOR_MEDIA, reliability=0.6, novelty=1.0, impact=0.5,
                      direction=0.5, sns_only=sns_only, injection_flagged=injection_flagged,
                      published_at=AT)


def test_new_product_headline_is_classified():
    events = GrowthCatalystTracker().detect([_signal("Acme unveils new product line")])
    assert len(events) == 1
    assert events[0].catalyst_type is CatalystType.NEW_PRODUCT
    assert events[0].tickers == ("AAPL",)


def test_major_contract_headline_is_classified():
    events = GrowthCatalystTracker().detect(
        [_signal("Acme signs major multi-year deal with new client")])
    assert any(e.catalyst_type is CatalystType.MAJOR_CONTRACT for e in events)


def test_a_headline_matching_no_category_produces_no_events():
    events = GrowthCatalystTracker().detect([_signal("Acme holds routine shareholder meeting")])
    assert events == []


def test_a_headline_can_match_multiple_categories():
    events = GrowthCatalystTracker().detect(
        [_signal("Acme unveils new product and announces strategic partnership")])
    types = {e.catalyst_type for e in events}
    assert CatalystType.NEW_PRODUCT in types
    assert CatalystType.KEY_PARTNERSHIP in types


def test_sns_only_and_injection_flags_are_carried_through_not_dropped():
    """§18/§19: a catalyst built from a quarantined signal must inherit the
    same quarantine, not read as a clean, tradeable catalyst."""
    sns_event = GrowthCatalystTracker().detect(
        [_signal("unveils new product", sns_only=True)])[0]
    assert sns_event.sns_only
    assert not sns_event.tradeable

    flagged_event = GrowthCatalystTracker().detect(
        [_signal("unveils new product", injection_flagged=True)])[0]
    assert flagged_event.injection_flagged
    assert not flagged_event.tradeable

    clean_event = GrowthCatalystTracker().detect([_signal("unveils new product")])[0]
    assert clean_event.tradeable
