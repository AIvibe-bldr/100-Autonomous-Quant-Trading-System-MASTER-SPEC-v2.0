"""Per-agent LLM usage metering (MASTER SPEC §80-83: AI cost visibility).

Skeptic and Pre-Trade Audit can share one model (§25), so usage is keyed by
`AgentModel.agent_id`, never by model name. `MeteredClient` wraps a provider
SDK client for ONE agent and records every call's token usage and every
failure, so the adapters themselves need no changes and cannot forget to
report.

Prices are NOT built in: provider prices change and a wrong built-in number
would silently misstate costs. Supply them via `QUANT_LLM_PRICES`
(JSON: {"model-id": [usd_per_1M_input, usd_per_1M_output]}) or the
constructor. Without a price, tokens are still counted and cost reads as
unknown (None), never 0.

Spending cap: every call passes `MeteredClient`, which asks the ledger for
permission first. Once the run's accumulated cost reaches the budget
(`QUANT_LLM_BUDGET_USD`, default $1.00 per ledger) further calls raise
`LLMBudgetExceeded`; all callers already fail closed on an LLM error
(Decision → no trade, Skeptic → veto, Audit → unavailable). A call that
starts under the cap may finish over it, so the cap can be exceeded by at most
one call. A model with no configured price cannot be metered, so while a cap is
active its calls are refused rather than allowed to spend invisibly.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional


DEFAULT_BUDGET_USD = 1.00


class LLMBudgetExceeded(RuntimeError):
    """The run's LLM spending cap is reached (or cannot be metered)."""


def budget_from_env() -> float:
    raw = os.environ.get("QUANT_LLM_BUDGET_USD", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET_USD
    # a malformed / non-positive / non-finite value must not silently uncap
    return value if value > 0 and math.isfinite(value) else DEFAULT_BUDGET_USD


@dataclass
class AgentUsage:
    agent_id: str
    model: str
    calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None      # None = no price configured


def prices_from_env() -> dict[str, tuple[float, float]]:
    raw = os.environ.get("QUANT_LLM_PRICES", "")
    if not raw:
        return {}
    try:
        return {m: (float(p[0]), float(p[1])) for m, p in json.loads(raw).items()}
    except (ValueError, TypeError, KeyError, IndexError):
        return {}   # malformed price config must not break trading


class LLMUsageLedger:
    def __init__(self, prices: Optional[dict[str, tuple[float, float]]] = None,
                 on_cost: Optional[Callable[[datetime, str, float], None]] = None,
                 budget_usd: Optional[float] = None) -> None:
        self.prices = prices if prices is not None else prices_from_env()
        # None → env / default cap; pass float("inf") to run uncapped on purpose
        self.budget_usd = budget_usd if budget_usd is not None else budget_from_env()
        # callback(at, agent_id, cost_usd) lets the caller feed the cost engine
        # without this shared module depending on services/*
        self._on_cost = on_cost
        self._agents: dict[str, AgentUsage] = {}

    def _row(self, agent_id: str, model: str) -> AgentUsage:
        return self._agents.setdefault(agent_id, AgentUsage(agent_id=agent_id, model=model))

    def record_call(self, agent_id: str, model: str, input_tokens: int,
                    output_tokens: int) -> None:
        row = self._row(agent_id, model)
        row.calls += 1
        row.input_tokens += input_tokens
        row.output_tokens += output_tokens
        price = self.prices.get(model)
        if price is not None:
            cost = (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
            row.cost_usd = (row.cost_usd or 0.0) + cost
            if self._on_cost is not None:
                self._on_cost(datetime.now(timezone.utc), agent_id, cost)

    def check_budget(self, agent_id: str, model: str) -> None:
        if math.isinf(self.budget_usd):
            return
        if model not in self.prices:
            raise LLMBudgetExceeded(
                f"{agent_id}: no price configured for {model!r} (QUANT_LLM_PRICES), "
                f"so spending cannot be capped — call refused")
        spent = self.total_cost_usd() or 0.0
        if spent >= self.budget_usd:
            raise LLMBudgetExceeded(
                f"LLM budget ${self.budget_usd:.2f} reached (spent ${spent:.4f}) — "
                f"call refused for {agent_id}")

    def record_failure(self, agent_id: str, model: str) -> None:
        self._row(agent_id, model).failures += 1

    def snapshot(self) -> list[AgentUsage]:
        return sorted(self._agents.values(), key=lambda r: r.agent_id)

    def total_cost_usd(self) -> Optional[float]:
        costs = [r.cost_usd for r in self._agents.values() if r.cost_usd is not None]
        return sum(costs) if costs else None


def _tokens(usage: Any) -> tuple[int, int]:
    # Anthropic: input_tokens/output_tokens; OpenAI: prompt_tokens/completion_tokens
    def pick(*names: str) -> int:
        for n in names:
            v = getattr(usage, n, None)
            if isinstance(v, int):
                return v
        return 0
    return pick("input_tokens", "prompt_tokens"), pick("output_tokens", "completion_tokens")


class MeteredClient:
    """Transparent proxy over an SDK client, bound to a single agent."""

    def __init__(self, inner: Any, ledger: LLMUsageLedger, agent_id: str, model: str) -> None:
        self._inner, self._ledger = inner, ledger
        self._agent_id, self._model = agent_id, model

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if callable(attr):
            return self._wrap_call(attr)
        if attr is None or isinstance(attr, (str, int, float, bool)):
            return attr
        return MeteredClient(attr, self._ledger, self._agent_id, self._model)

    def _wrap_call(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self._ledger.check_budget(self._agent_id, self._model)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                self._ledger.record_failure(self._agent_id, self._model)
                raise
            usage = getattr(result, "usage", None)
            if usage is not None:
                i, o = _tokens(usage)
                self._ledger.record_call(self._agent_id, self._model, i, o)
            return result
        return call
