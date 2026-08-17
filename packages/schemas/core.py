"""Core schemas (MASTER SPEC §8, §28, §33, §45).

Every AI-facing structure is a strict pydantic model: unknown fields are
rejected, numeric bounds are enforced, and malformed payloads raise —
"Malformed Output: Reject" (§28) and "AI出力はSchema Validation必須" (§74).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.common.environment import Environment


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Market data (§8)
# ---------------------------------------------------------------------------

class Bar(StrictModel):
    symbol: str
    ts: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    vwap: Optional[float] = Field(default=None, gt=0)
    source: str = "unknown"
    data_version: str = "1"

    @model_validator(mode="after")
    def _ohlc_sane(self) -> "Bar":
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"impossible OHLC for {self.symbol}@{self.ts}")
        return self


class Quote(StrictModel):
    symbol: str
    ts: datetime
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)
    halted: bool = False
    source: str = "unknown"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def crossed(self) -> bool:
        """Bid > Ask is a data-integrity anomaly (§11); kept representable so the
        Data Integrity Engine can inspect and quarantine it."""
        return self.bid > self.ask


# ---------------------------------------------------------------------------
# Decision AI output (§28) — JSON Schema fixed
# ---------------------------------------------------------------------------

class Action(str, enum.Enum):
    """Order side — what can actually be sent to a broker (§2).

    Deliberately only two values: an order is a BUY or a SELL. Stances that
    produce no order (HOLD/WAIT/NO_TRADE/AVOID) live in `DecisionAction`, so
    a non-order stance cannot accidentally be constructed as an order side.
    """

    BUY = "BUY"
    SELL = "SELL"


class DecisionAction(str, enum.Enum):
    """What the Decision AI concluded (§2 of the role spec).

    Meanings are fixed and enforced downstream:
      BUY      — open a new long, or add to an approved existing long
      SELL     — reduce or close an EXISTING long only. Never a short: the
                 Master Risk Controller enforces sell_qty <= current_long_qty,
                 and a SELL on an unheld symbol is rejected outright.
      HOLD     — keep the current long; emits no order
      WAIT     — no order now; re-evaluate after a condition or interval
      NO_TRADE — no sufficient edge right now
      AVOID    — excluded for a period due to risk / data quality / event risk
    """

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    AVOID = "AVOID"

    @property
    def is_order(self) -> bool:
        """True only for stances that produce a broker order."""
        return self in (DecisionAction.BUY, DecisionAction.SELL)

    def to_order_side(self) -> "Action":
        """Convert to an order side; raises for non-order stances so a HOLD
        can never be mistaken for a tradeable instruction."""
        if not self.is_order:
            raise ValueError(f"{self.value} produces no order and has no side")
        return Action.BUY if self is DecisionAction.BUY else Action.SELL


class ScenarioCase(StrictModel):
    description: str
    target_price: float = Field(gt=0)
    probability: float = Field(ge=0.0, le=1.0)


class DecisionOutput(StrictModel):
    """Fixed decision schema (§28). Point forecasts are forbidden (§32):
    bull/base/bear each carry a target and probability."""

    symbol: str = Field(min_length=1, max_length=12)
    action: DecisionAction   # 6 stances; only BUY/SELL produce an order
    confidence: float = Field(ge=0.0, le=1.0)
    expected_horizon: str  # e.g. "1d" | "1w" | "1m" | "3m" | "6m"
    expected_return_range: tuple[float, float]
    bull_case: ScenarioCase
    base_case: ScenarioCase
    bear_case: ScenarioCase
    key_evidence: list[str]
    counter_evidence: list[str]
    risk_factors: list[str]
    invalidation_conditions: list[str]
    unknowns: list[str]
    decision_version: str

    @model_validator(mode="after")
    def _range_ordered(self) -> "DecisionOutput":
        lo, hi = self.expected_return_range
        if lo > hi:
            raise ValueError("expected_return_range must be (low, high)")
        total_p = self.bull_case.probability + self.base_case.probability + self.bear_case.probability
        if not (0.99 <= total_p <= 1.01):
            raise ValueError(f"scenario probabilities must sum to 1.0, got {total_p:.3f}")
        return self


class SkepticOutput(StrictModel):
    """Skeptic AI output (§29): reasons NOT to take the trade."""

    proposal_symbol: str
    objections: list[str]
    severity: float = Field(ge=0.0, le=1.0)
    recommends_veto: bool
    model_family: str


# ---------------------------------------------------------------------------
# Trade proposal pipeline (§7, §33, §35)
# ---------------------------------------------------------------------------

class ProposalSource(str, enum.Enum):
    AI = "AI"
    HUMAN = "HUMAN"  # §76-78: human intent flows through the same pipeline


class TradeProposal(StrictModel):
    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    side: Action  # order side; `Action` itself is BUY/SELL only
    source: ProposalSource
    decision: DecisionOutput
    skeptic: Optional[SkepticOutput] = None
    created_at: datetime

    @model_validator(mode="after")
    def _side_matches_decision(self) -> "TradeProposal":
        """A proposal may only exist for an order-producing stance, and its
        side must match what the Decision AI actually concluded — a HOLD or
        WAIT can never become an order, and a BUY decision can never become
        a SELL order here (that mismatch is also caught by Pre-Trade Audit)."""
        if not self.decision.action.is_order:
            raise ValueError(
                f"{self.decision.action.value} produces no order and cannot become a proposal")
        if self.decision.action.to_order_side() is not self.side:
            raise ValueError(
                f"proposal side {self.side.value} contradicts decision "
                f"{self.decision.action.value}")
        return self


class StopType(str, enum.Enum):
    HARD = "HARD"
    ATR = "ATR"
    VOLATILITY = "VOLATILITY"
    THESIS = "THESIS"
    FLOW = "FLOW"
    NEWS = "NEWS"
    TIME = "TIME"
    TRAILING = "TRAILING"
    PORTFOLIO = "PORTFOLIO"


class StopPlan(StrictModel):
    """Loss & Exit plan, designed BEFORE entry (§33)."""

    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    stop_type: StopType
    stop_reason: str = Field(min_length=1)
    holding_horizon: str
    thesis: str = Field(min_length=1)
    invalidation: str = Field(min_length=1)
    profit_target: float = Field(gt=0)
    gap_risk_score: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _stop_below_entry_for_long(self) -> "StopPlan":
        if self.stop_price >= self.entry_price:
            # V1 is long-only (no short selling §2), so stop must sit below entry
            raise ValueError("long-only V1: stop_price must be below entry_price")
        return self

    @property
    def stop_distance(self) -> float:
        return self.entry_price - self.stop_price


class SizedProposal(StrictModel):
    proposal: TradeProposal
    stop_plan: StopPlan
    qty: float = Field(gt=0)
    risk_amount: float = Field(ge=0)   # money lost if stop hits (before gap)
    notional: float = Field(gt=0)
    calibrated_confidence: float = Field(ge=0.0, le=1.0)
    sizing_version: str


# ---------------------------------------------------------------------------
# Orders (§45-46) and risk approval (§42)
# ---------------------------------------------------------------------------

class OrderState(str, enum.Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # 重大状態: triggers reconciliation (§45)


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"  # protective stop orders remain allowed under MASTER STOP (§43)


class OrderIntent(StrictModel):
    client_order_id: str = Field(min_length=8)  # unique, idempotency key (§46)
    proposal_id: str
    symbol: str
    side: Action
    qty: float = Field(gt=0)
    order_type: OrderType
    limit_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    environment: Environment
    is_protective_exit: bool = False  # protective stop / risk-reducing SELL (§43)
    created_at: datetime


class RiskCheck(StrictModel):
    name: str
    passed: bool
    detail: str = ""


class RiskApproval(StrictModel):
    """Deterministic approval token minted only by MasterRiskController (§42).

    The signature binds the approval to the exact order parameters; the
    Execution Engine independently verifies it, so nothing upstream of the
    risk controller (including any AI) can conjure an executable order (§7).
    """

    approval_id: str
    client_order_id: str
    symbol: str
    side: Action
    qty: float
    checks: tuple[RiskCheck, ...]
    risk_state: str
    approved_at: datetime
    signature: str


class RiskRejection(StrictModel):
    client_order_id: str
    checks: tuple[RiskCheck, ...]
    reasons: tuple[str, ...]
    rejected_at: datetime


class RiskApprovedOrder(StrictModel):
    intent: OrderIntent
    approval: RiskApproval

    @model_validator(mode="after")
    def _approval_matches_intent(self) -> "RiskApprovedOrder":
        a, i = self.approval, self.intent
        if (a.client_order_id, a.symbol, a.side, a.qty) != (i.client_order_id, i.symbol, i.side, i.qty):
            raise ValueError("risk approval does not match order intent")
        return self


# ---------------------------------------------------------------------------
# Broker layer (§48, §100)
# ---------------------------------------------------------------------------

class BrokerOrderRequest(StrictModel):
    client_order_id: str
    symbol: str
    side: Action
    qty: float = Field(gt=0)
    order_type: OrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None


class BrokerAck(StrictModel):
    client_order_id: str
    broker_order_id: str
    accepted: bool
    reason: str = ""


class BrokerFill(StrictModel):
    broker_fill_id: str
    client_order_id: str
    symbol: str
    side: Action
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    fees: float = Field(ge=0)
    ts: datetime


class BrokerOrderStatus(StrictModel):
    client_order_id: str
    broker_order_id: str
    state: OrderState
    filled_qty: float = Field(ge=0)
    remaining_qty: float = Field(ge=0)


class BrokerPosition(StrictModel):
    symbol: str
    qty: float
    avg_cost: float = Field(ge=0)


class BrokerCashBalance(StrictModel):
    settled_cash: float = Field(ge=0)
    unsettled_cash: float = Field(ge=0)
    currency: str = "USD"

    @property
    def buying_power(self) -> float:
        """Cash account: only settled cash is spendable (§14, ISSUE-2)."""
        return self.settled_cash
