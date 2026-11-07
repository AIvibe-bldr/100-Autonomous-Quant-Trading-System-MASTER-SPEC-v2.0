"""Independent Audit AI receives the current quote (and the entry drift) like Decision does."""
from datetime import date, datetime, time, timezone

from packages.common.clock import FrozenClock
from services.decision.audit import AuditContext
from services.decision.claude_adapters import _build_audit_prompt
from services.market_data.universe import UniverseManager, UniverseSymbol
from tests.conftest import SYMBOLS, build_pipeline


def _run():
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    p = build_pipeline(clock, universe)
    calls = []
    real = p.auditor.model.audit
    p.auditor.model.audit = lambda d, s, i, c: (calls.append((d, s, i, c)), real(d, s, i, c))[1]
    p.run_session(clock.now())
    return p, calls


def test_pipeline_gives_the_auditor_a_fresh_quote_with_its_decision_time():
    _, calls = _run()
    assert calls, "no order reached the audit"
    for _, sized, intent, ctx in calls:
        assert ctx.quote is not None and ctx.quote.symbol == intent.symbol
        assert ctx.quote_as_of is not None


def test_audit_prompt_shows_quote_age_and_computed_drift_from_planned_entry():
    _, calls = _run()
    d, sized, intent, ctx = calls[0]
    prompt = _build_audit_prompt(d, sized, intent, ctx)
    assert "Current quote (" in prompt and "spread" in prompt and "quote age" in prompt
    assert f"Planned entry (ask when the trade was decided): {sized.stop_plan.entry_price:.4f}" in prompt
    drift = (ctx.quote.ask - sized.stop_plan.entry_price) / sized.stop_plan.entry_price
    assert f"{drift:+.2%}" in prompt


def test_audit_prompt_says_plainly_when_no_quote_is_available():
    _, calls = _run()
    d, sized, intent, ctx = calls[0]
    bare = AuditContext(now=ctx.now, signal_age=ctx.signal_age)
    prompt = _build_audit_prompt(d, sized, intent, bare)
    assert "Current quote: not available" in prompt and "Planned entry" not in prompt
