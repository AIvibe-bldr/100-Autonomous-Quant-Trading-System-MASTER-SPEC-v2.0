"""MASTER_SPEC audit section C: `reconciliation_ok` and `correlation_to_book`
were literals in the live flow. (1) each session reconciles ledger vs broker
first and a mismatch blocks new entries; (2) the sizing engine is given the
real max |correlation| of the candidate against current holdings."""
from datetime import date, datetime, time, timezone

import pytest

from packages.common.clock import FrozenClock
from services.market_data.service import MockProvider
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.position_sizing.correlation import max_abs_correlation_to_book
from services.risk.master_controller import RiskState
from tests.conftest import SYMBOLS, build_pipeline


# ---- pure function --------------------------------------------------------

def _series(*xs):
    return list(xs)


def test_no_holdings_means_zero_correlation():
    assert max_abs_correlation_to_book([0.01, -0.02, 0.03] * 8, {}) == 0.0


def test_identical_and_inverse_series_are_fully_correlated():
    cand = [0.01, -0.02, 0.03, 0.005, -0.01, 0.02] * 4
    assert max_abs_correlation_to_book(cand, {"A": cand}) == pytest.approx(1.0)
    assert max_abs_correlation_to_book(cand, {"A": [-x for x in cand]}) == pytest.approx(1.0)


def test_takes_the_maximum_over_holdings():
    cand = [0.01, -0.02, 0.03, 0.005, -0.01, 0.02] * 4
    other = [0.02, 0.01, -0.01, 0.03, 0.0, -0.02] * 4
    both = max_abs_correlation_to_book(cand, {"A": other, "B": cand})
    assert both == pytest.approx(1.0)


def test_unmeasurable_holding_is_treated_as_fully_correlated_not_zero():
    cand = [0.01, -0.02, 0.03] * 8
    assert max_abs_correlation_to_book(cand, {"A": [0.01, 0.02]}) == 1.0     # too short
    assert max_abs_correlation_to_book(cand, {"A": [0.0] * 24}) == 1.0       # zero variance


# ---- pipeline wiring -------------------------------------------------------

def _pipeline(provider=None):
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return build_pipeline(clock, universe, provider=provider), clock


def test_sizing_receives_the_computed_correlation_against_holdings(monkeypatch):
    import services.pipeline as pipeline_module
    p, clock = _pipeline()
    p.run_session(clock.now())
    assert p.ledger.positions, "setup: need a held position"
    received = {}
    monkeypatch.setattr(pipeline_module, "max_abs_correlation_to_book",
                        lambda cand, holdings: (received.update(holdings), 0.9)[1])
    seen = []
    real = p.sizing.size
    p.sizing.size = lambda proposal, stop, quote, pctx, cal: (
        seen.append(pctx.correlation_to_book), real(proposal, stop, quote, pctx, cal))[1]
    clock.advance(86400)
    p.run_session(clock.now())
    assert seen and all(c == pytest.approx(0.9) for c in seen)
    assert received and all(len(v) > 15 for v in received.values())   # real return history


def test_first_entry_with_empty_book_has_zero_correlation():
    p, clock = _pipeline()
    seen = []
    real = p.sizing.size
    p.sizing.size = lambda proposal, stop, quote, pctx, cal: (
        seen.append(pctx.correlation_to_book), real(proposal, stop, quote, pctx, cal))[1]
    p.run_session(clock.now())
    assert seen and seen[0] == 0.0


def test_ledger_broker_mismatch_halts_entries_before_any_decision_call():
    p, clock = _pipeline()
    p.run_session(clock.now())
    p.ledger.deposit(500.0, note="drift: ledger no longer matches the broker")
    calls = []
    real = p.decision_model.decide
    p.decision_model.decide = lambda ctx: (calls.append(1), real(ctx))[1]
    clock.advance(86400)
    result = p.run_session(clock.now())
    assert p.risk_controller.state is RiskState.HALT_NEW_ENTRIES
    assert calls == [] and any("reconciliation" in r for r in result.no_trade_reasons)


def test_consistent_book_does_not_block_trading():
    p, clock = _pipeline()
    assert p.run_session(clock.now()).fills
    assert p.risk_controller.state is RiskState.NORMAL
