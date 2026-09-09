"""Capital Cell schema (docs/capital_cell_architecture.md §1-3, §41 priority 1).

A Capital Cell is a virtual sub-portfolio inside ONE Master Broker Account —
never a broker account of its own (§3). It exists so multiple independent
alpha strategies can be sized, stopped, and evaluated separately while still
settling through the single deterministic Master Risk Controller / Execution
Engine / Broker this repo already has (§7, §42).

Deliberately absent: any field carrying the Master Portfolio's 100x Challenge
progress. §1 forbids that progress from ever reaching Cell-level sizing, and
§39 lists `target_progress_does_not_change_cell_risk_budget` as an invariant
to test — the cheapest way to guarantee it here is to give it nowhere to go.
"""
from __future__ import annotations

import enum
from datetime import datetime

from pydantic import Field

from packages.schemas.core import Action, StrictModel


class CellStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"      # can generate new Cell Order Intents
    PAUSED = "PAUSED"      # no new entries; existing virtual positions still tracked/reconciled
    RETIRED = "RETIRED"    # fully closed out; kept for history/attribution


class CapitalCell(StrictModel):
    """Identity + alpha hypothesis for one cell (§1, §30). Evaluated on Net
    Expected Edge / Risk-adjusted Return / Drawdown / Capacity / Correlation /
    Tail Risk / Decision Quality / Stop Quality / Execution Quality / Alpha
    Stability — never on distance to the Master Portfolio's 100x goal."""

    cell_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    alpha_hypothesis: str = Field(min_length=1)
    status: CellStatus = CellStatus.ACTIVE


class CellOrderIntent(StrictModel):
    """A cell's own order intent, BEFORE Internal Netting (§5).

    Distinct from `packages.schemas.core.OrderIntent`, which only exists for
    the single already-netted Master order that reaches Pre-Trade Audit AI /
    Master Risk Controller / Execution Engine. A CellOrderIntent never
    reaches a broker directly — the Netting Engine (services.capital_cells)
    consumes a batch of these and produces the Master order(s), if any."""

    intent_id: str = Field(min_length=1)   # unique per intent (§13)
    cell_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Action
    qty: float = Field(gt=0)
    created_at: datetime


class StopScope(str, enum.Enum):
    """How far a Stop/Invalidation reaches (§15). The scope decides which
    cells' positions get an exit intent when the stop fires — never whether
    a firing stop's OWN cell is exited (that always happens)."""

    CELL = "CELL"            # only the strategy that owns this stop is wrong
    SYMBOL = "SYMBOL"        # something is wrong with the symbol itself —
                              # every cell holding it exits, regardless of
                              # each cell's own thesis or stop level
    PORTFOLIO = "PORTFOLIO"  # portfolio-wide risk event — every cell, every
                              # symbol, exits


class CellStopPlan(StrictModel):
    """One cell's protective stop / invalidation level (§14-15). Tracked per
    cell even when several cells hold the same symbol for different reasons —
    the Master Broker Position is one aggregate, but the reason for holding
    it, and the price at which that reason is invalidated, is not."""

    stop_id: str = Field(min_length=1)
    cell_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    stop_price: float = Field(gt=0)
    scope: StopScope
    reason: str = Field(min_length=1)
