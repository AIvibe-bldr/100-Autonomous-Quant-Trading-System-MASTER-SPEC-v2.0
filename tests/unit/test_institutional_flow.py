"""Institutional flow (§20): honest sources, rendered to the AI, never a reason to trade alone."""
from datetime import date, datetime, timedelta, timezone

import pytest

from packages.common.clock import FrozenClock
from packages.schemas.core import Bar
from services.decision.claude_adapters import _build_skeptic_prompt
from services.decision.prompts import build_decision_prompt
from services.institutional.engine import FlowFeature, FlowObservation, InstitutionalFlowEngine
from services.institutional.sources import (
    FILINGS_LOOKBACK_DAYS,
    EdgarOwnershipFilingsSource,
    FlowUnavailable,
    describe_flow,
    volume_anomaly,
)
from services.market_data.edgar_client import EdgarClient, EdgarUnavailable
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline

T = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _bars(volumes, closes=None):
    closes = closes or [100.0] * len(volumes)
    return [Bar(symbol="AAA", ts=T - timedelta(days=len(volumes) - i), open=c, high=c, low=c, close=c,
                volume=v) for i, (v, c) in enumerate(zip(volumes, closes))]


def test_volume_anomaly_needs_history_and_a_big_enough_ratio():
    assert volume_anomaly("AAA", _bars([1000] * 10)) is None                          # too little history
    assert volume_anomaly("AAA", _bars([1000] * 20 + [1400])) is None                 # 1.4x < 1.5x
    obs, ratio = volume_anomaly("AAA", _bars([1000] * 20 + [2000], [100.0] * 20 + [103.0]))
    assert ratio == pytest.approx(2.0) and obs.feature is FlowFeature.VOLUME_ANOMALY
    assert obs.value == pytest.approx(0.5) and obs.source == "own-bars"               # up day, (2-1)/2


def test_the_direction_comes_from_the_price_move_and_is_capped():
    down, _ = volume_anomaly("AAA", _bars([1000] * 20 + [9000], [100.0] * 20 + [97.0]))
    assert down.value == -1.0
    flat, _ = volume_anomaly("AAA", _bars([1000] * 20 + [2000]))
    assert flat.value == 0.0


def test_describe_flow_states_what_it_saw_and_what_it_did_not():
    spike = _bars([1000] * 20 + [2500], [100.0] * 20 + [104.0])
    lines, obs = describe_flow("AAA", spike, ["2026-08-01 SCHEDULE 13G/A filed (x)"])
    assert obs is not None and "2.5x" in lines[0] and "+4.0%" in lines[0]
    assert lines[1].startswith("ownership filing: 2026-08-01")
    calm, obs2 = describe_flow("AAA", _bars([1000] * 21), [])
    assert obs2 is None and "none (latest volume 1.0x" in calm[0]
    assert f"none in the last {FILINGS_LOOKBACK_DAYS} days" in calm[1]
    assert describe_flow("AAA", _bars([1000] * 5), None)[0] == ["volume anomaly: not enough history to judge"]


def test_one_feature_is_never_actionable():
    eng = InstitutionalFlowEngine()
    eng.ingest(FlowObservation("AAA", FlowFeature.VOLUME_ANOMALY, 1.0, T, "own-bars"))
    sig = eng.signal("AAA")
    assert sig.score == 1.0 and not sig.actionable                                    # §20


# ---- the filings source ----

def _client(forms):
    rows = [(f"0000000001-26-{i:06d}", d, f"{d}T20:00:00.000Z", form, "", "x.htm")
            for i, (form, d) in enumerate(forms)]

    def get(url, headers):
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": 1, "ticker": "AAA", "title": "A"}}
        if url.endswith("CIK0000000001.json"):
            keys = ["accessionNumber", "filingDate", "acceptanceDateTime", "form", "items", "primaryDocument"]
            return {"filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}, "files": []}}
        raise EdgarUnavailable("SEC HTTP 404")
    return EdgarClient("QuantPaper ops@example.org", http_get=get, sleep=lambda s: None, predecessors={})


def test_only_recent_ownership_forms_are_reported_as_facts_without_a_direction():
    c = _client([("SCHEDULE 13G/A", "2026-08-01"), ("SCHEDULE 13D", "2026-07-20"), ("8-K", "2026-08-05"),
                 ("SCHEDULE 13G", "2026-01-10"), ("4", "2026-08-02")])               # old 13G, 8-K, Form 4: excluded
    out = EdgarOwnershipFilingsSource(c).fetch(["AAA"], date(2026, 8, 11))
    assert len(out["AAA"]) == 2
    assert "SCHEDULE 13G/A" in out["AAA"][0] and "does not say whether the holder bought or sold" in out["AAA"][0]


def test_an_edgar_failure_is_flow_unavailable_not_an_empty_list():
    def boom(url, headers):
        raise EdgarUnavailable("SEC HTTP 503")
    c = EdgarClient("QuantPaper ops@example.org", http_get=boom, sleep=lambda s: None)
    with pytest.raises(FlowUnavailable):
        EdgarOwnershipFilingsSource(c).fetch(["AAA"], date(2026, 8, 11))


# ---- through the pipeline into the prompts ----

class _Src:
    def __init__(self, fail=False):
        self.fail = fail

    def fetch(self, symbols, today):
        if self.fail:
            raise FlowUnavailable("down")
        return {s: ["2026-08-01 SCHEDULE 13G filed (ownership report)"] for s in symbols}


def _run(src):
    clock = FrozenClock(current=T)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe, flow_filings_source=src)
    seen = []
    real = p.decision_model.decide
    p.decision_model.decide = lambda ctx: (seen.append(ctx), real(ctx))[1]
    return p.run_session(T), seen


def test_decision_and_skeptic_prompts_carry_the_flow_facts():
    result, seen = _run(_Src())
    ctx = seen[0]
    assert any(x.startswith("volume anomaly") for x in ctx.flow_lines) and ctx.flow_filings_fetched
    from tests.unit.helpers import make_decision
    for prompt in (build_decision_prompt(ctx), _build_skeptic_prompt(make_decision(ctx.scan.symbol), ctx)):
        assert "Institutional / flow indicators:" in prompt
        assert "ownership filing: 2026-08-01 SCHEDULE 13G" in prompt
        assert "block trades, order-flow imbalance, options flow" in prompt        # what is NOT available


def test_unavailable_filings_are_stated_as_unavailable_and_warned_about():
    result, seen = _run(_Src(fail=True))
    assert not seen[0].flow_filings_fetched
    assert "ownership filings (SEC 13D/13G): not available" in build_decision_prompt(seen[0])
    assert any("ownership filings unavailable" in w for w in result.data_warnings)


def test_without_a_filings_source_the_volume_facts_still_reach_the_model():
    result, seen = _run(None)
    assert seen[0].flow_lines and not seen[0].flow_filings_fetched
    assert result.data_warnings == []                       # not connected is not a failure
