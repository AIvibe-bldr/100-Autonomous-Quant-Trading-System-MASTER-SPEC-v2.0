"""Session observers: the one way learning/measurement code listens to the pipeline.

An observer is handed plain, read-only facts about what just happened (a decision, an exit,
the end of a session) and may record, grade and summarise them. It receives no handle on the
pipeline, the risk controller, the broker or any configuration, so it cannot change what the
system does — the learning layers' "propose, never apply" rule (MASTER SPEC §23, §52, §98)
holds by construction. The pipeline guards every call: an observer that raises is reported as
a data warning and never interrupts trading.

Observers that need to look at risk state or config do not belong here; this module (and the
observers in services/pdca) must stay free of those imports (tests/unit/test_architecture_boundaries.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

from packages.schemas.core import Action, DecisionOutput

if TYPE_CHECKING:  # no runtime dependency on the pipeline module (it imports this one)
    from services.decision.models import DecisionContext
    from services.pipeline import PipelineResult

PriceOn = Callable[[str, datetime], Optional[float]]     # (symbol, time) -> last close at/before, or None


@dataclass(frozen=True)
class DecisionEvent:
    decision_id: str
    symbol: str
    as_of: datetime
    decision: DecisionOutput
    price: Optional[float]               # the quote mid the decision was made against
    context: "DecisionContext"


@dataclass(frozen=True)
class FillEvent:
    """A fill the pipeline has just booked. `reference_price` is what the plan assumed: the
    planned entry (ask) for a BUY, the stop trigger price for a protective SELL; None if unknown."""
    symbol: str
    side: Action
    qty: float
    price: float
    fees: float
    ts: datetime
    client_order_id: str
    reference_price: Optional[float]
    source: Optional[str] = None          # who originated an entry: "AI" or "HUMAN" (None = not known here)


@dataclass(frozen=True)
class OverrideEvent:
    """A human chose to buy a symbol (§79). `ai_action` is what the Decision AI said about the same
    symbol this session (None if it could not be asked); `traded` is whether an order resulted."""
    symbol: str
    as_of: datetime
    ai_action: Optional[str]
    human_action: str
    entry_price: Optional[float]
    traded: bool


@dataclass(frozen=True)
class SessionEndEvent:
    as_of: datetime
    result: "PipelineResult"
    price_on: PriceOn
    equity: float                         # marked at session time, after the session's trades
    # the books as plain numbers, for observers that audit them (e.g. the capital-cell mirror)
    ledger_cash: float = 0.0
    ledger_positions: Optional[dict[str, float]] = None


class SessionObserver:
    """Base class: override the hooks you need. `summary()` must be JSON-serialisable."""

    name = "observer"

    def on_decision(self, event: DecisionEvent) -> None:
        return None

    def on_fill(self, event: FillEvent) -> None:
        return None

    def on_human_override(self, event: OverrideEvent) -> None:
        return None

    def on_session_end(self, event: SessionEndEvent) -> None:
        return None

    def summary(self) -> dict[str, Any]:
        return {}
