"""Per-agent LLM usage metering (§80-83)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from packages.common.llm_usage import (DEFAULT_BUDGET_USD, LLMBudgetExceeded, LLMUsageLedger,
                                       MeteredClient, budget_from_env, prices_from_env)


class _Sub:
    def create(self, fail=False, shape="anthropic"):
        if fail:
            raise RuntimeError("boom")
        u = (SimpleNamespace(input_tokens=100, output_tokens=50) if shape == "anthropic"
             else SimpleNamespace(prompt_tokens=100, completion_tokens=50))
        return SimpleNamespace(usage=u)


def _client():
    return SimpleNamespace(messages=_Sub())


def test_anthropic_and_openai_usage_shapes():
    led = LLMUsageLedger(prices={}, budget_usd=float("inf"))
    MeteredClient(_client(), led, "a:x", "m").messages.create()
    MeteredClient(_client(), led, "b:x", "m").messages.create(shape="openai")
    rows = {r.agent_id: r for r in led.snapshot()}
    assert (rows["a:x"].input_tokens, rows["a:x"].output_tokens) == (100, 50)
    assert (rows["b:x"].input_tokens, rows["b:x"].output_tokens) == (100, 50)


def test_unknown_price_is_none_not_zero():
    led = LLMUsageLedger(prices={}, budget_usd=float("inf"))
    MeteredClient(_client(), led, "a", "m").messages.create()
    assert led.snapshot()[0].cost_usd is None and led.total_cost_usd() is None


def test_cost_and_callback_when_price_known():
    seen = []
    led = LLMUsageLedger(prices={"m": (10.0, 20.0)}, on_cost=lambda at, a, c: seen.append((a, c)))
    MeteredClient(_client(), led, "a", "m").messages.create()
    assert led.snapshot()[0].cost_usd == pytest.approx((100 * 10 + 50 * 20) / 1e6)
    assert seen == [("a", pytest.approx(0.002))]


def test_failures_counted_and_reraised():
    led = LLMUsageLedger(prices={}, budget_usd=float("inf"))
    with pytest.raises(RuntimeError):
        MeteredClient(_client(), led, "a", "m").messages.create(fail=True)
    r = led.snapshot()[0]
    assert (r.failures, r.calls) == (1, 0)


def test_shared_model_agents_stay_separate():
    led = LLMUsageLedger(prices={}, budget_usd=float("inf"))
    MeteredClient(_client(), led, "skeptic:p:m", "m").messages.create()
    MeteredClient(_client(), led, "audit:p:m", "m").messages.create()
    MeteredClient(_client(), led, "audit:p:m", "m").messages.create()
    assert {r.agent_id: r.calls for r in led.snapshot()} == {"skeptic:p:m": 1, "audit:p:m": 2}


def test_malformed_prices_env_is_safe(monkeypatch):
    monkeypatch.setenv("QUANT_LLM_PRICES", "{not json")
    assert prices_from_env() == {}
    monkeypatch.setenv("QUANT_LLM_PRICES", '{"m":[1]}')
    assert prices_from_env() == {}
    monkeypatch.setenv("QUANT_LLM_PRICES", '{"m":[1,2]}')
    assert prices_from_env() == {"m": (1.0, 2.0)}


def test_llm_usage_endpoint():
    from apps.api.main import create_app
    from datetime import datetime, time, timezone, date
    from packages.common.clock import FrozenClock
    from services.market_data.universe import UniverseManager, UniverseSymbol
    from tests.conftest import SYMBOLS, build_pipeline

    u = UniverseManager()
    for s in SYMBOLS:
        u.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    clock = FrozenClock(current=datetime.combine(date(2026, 8, 10), time(15, 0), tzinfo=timezone.utc))
    led = LLMUsageLedger(prices={"m": (1.0, 1.0)})
    MeteredClient(_client(), led, "a", "m").messages.create()
    body = TestClient(create_app(build_pipeline(clock, u), llm_usage=led)).get("/llm-usage").json()
    assert body["agents"][0]["agent_id"] == "a" and body["agents"][0]["calls"] == 1
    assert body["total_cost_usd"] == pytest.approx(150e-6)
    empty = TestClient(create_app(build_pipeline(clock, u))).get("/llm-usage").json()
    assert empty == {"agents": [], "total_cost_usd": None}


# --- spending cap -----------------------------------------------------------

def test_calls_are_refused_once_the_budget_is_reached_and_no_call_is_made():
    # one call costs (100*10 + 50*20)/1e6 = $0.002
    led = LLMUsageLedger(prices={"m": (10.0, 20.0)}, budget_usd=0.003)
    c = MeteredClient(_client(), led, "a:x", "m")
    c.messages.create()
    c.messages.create()          # starts under the cap ($0.002 < $0.003), may overshoot
    with pytest.raises(LLMBudgetExceeded):
        c.messages.create()
    assert led.snapshot()[0].calls == 2      # the refused call never reached the SDK


def test_budget_is_shared_across_agents():
    led = LLMUsageLedger(prices={"m": (10.0, 20.0)}, budget_usd=0.002)
    MeteredClient(_client(), led, "a:x", "m").messages.create()
    with pytest.raises(LLMBudgetExceeded):
        MeteredClient(_client(), led, "b:x", "m").messages.create()


def test_unpriced_model_is_refused_while_a_cap_is_active():
    led = LLMUsageLedger(prices={}, budget_usd=1.0)
    with pytest.raises(LLMBudgetExceeded):
        MeteredClient(_client(), led, "a:x", "m").messages.create()


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "nan", "inf"])
def test_bad_budget_env_falls_back_to_default_never_uncapped(monkeypatch, raw):
    monkeypatch.setenv("QUANT_LLM_BUDGET_USD", raw)
    assert budget_from_env() == DEFAULT_BUDGET_USD


def test_budget_env_is_honoured(monkeypatch):
    monkeypatch.setenv("QUANT_LLM_BUDGET_USD", "0.5")
    assert budget_from_env() == 0.5
