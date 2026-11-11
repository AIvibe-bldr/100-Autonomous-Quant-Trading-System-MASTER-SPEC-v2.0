"""Durable paper account: save it at consistent points, resume it after a restart (SAFETY_AUDIT F3/F4/F9).

The paper broker and the ledger live in memory, so a restart used to mean a NEW account. This captures the
state that makes the account what it is — the simulated broker (cash, positions, orders, fills), the ledger
(its entry log, positions, high-water mark), the anti-martingale memory, the execution engine's
"already-reported fills", and the pipeline's protective-stop registry — into `DurableCheckpointStore`.

WHEN: only at moments when the books agree with each other (after an order's fills AND its protective stop,
after exit fills, and around each session). A crash between two such moments loses the simulated work since
the last one — never a half-applied state: on restore, an order the store recorded but the restored broker
never saw is marked REJECTED ("rewound"), exactly as if it had not happened.

RESTORE IS VERIFIED, not trusted: the ledger is re-derived from its entries, the ledger must reconcile
against the restored broker, and every held position must have a live protective stop at the broker.
Anything else raises `CheckpointError` and nothing trades: a human decides (start a new account, or repair).

Only a SIMULATED broker is checkpointed. A real broker is the source of truth for its own account and is
reconciled, not restored — that path is separate and not built here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from packages.common.durable_store import CheckpointRecord, DurableCheckpointStore
from packages.schemas.core import OrderState
from services.pipeline import TradingPipeline
from services.risk.master_controller import RiskState

SCHEMA_VERSION = 1
_LIVE_STOPS = (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED)


class CheckpointError(RuntimeError):
    """The account cannot be (safely) restored. Nothing may trade on it."""


@dataclass
class RestoreReport:
    checkpoint_id: int
    checkpoint_at: str
    reason: str
    lost_orders: list[str] = field(default_factory=list)       # recorded in the order store, never seen by the broker
    interrupted_session: Optional[date] = None                 # a session started but did not finish


def capture(pipeline: TradingPipeline) -> dict:
    broker = pipeline.execution.broker
    if not hasattr(broker, "export_state"):
        raise CheckpointError("only a simulated broker can be checkpointed; a real broker is reconciled instead")
    return {"schema": SCHEMA_VERSION, "environment": pipeline.environment.value,
            "broker": broker.export_state(), "ledger": pipeline.ledger.export_state(),
            "sizing": pipeline.sizing.export_state(), "execution": pipeline.execution.export_state(),
            "registry": pipeline.export_registry(), "risk_state": pipeline.risk_controller.state.value}


class Checkpointer:
    """The callable handed to `TradingPipeline.checkpointer` and the runner."""

    def __init__(self, pipeline: TradingPipeline, store: DurableCheckpointStore) -> None:
        self._pipeline, self._store = pipeline, store

    def __call__(self, reason: str) -> None:
        self._store.save(capture(self._pipeline), reason, self._pipeline.clock.now(), SCHEMA_VERSION)


def restore(pipeline: TradingPipeline, record: CheckpointRecord) -> RestoreReport:
    """Rebuild `pipeline`'s account from a snapshot. `pipeline` must be freshly built (empty ledger/broker)."""
    state = record.state
    if record.schema_version != SCHEMA_VERSION or state.get("schema") != SCHEMA_VERSION:
        raise CheckpointError(f"checkpoint schema {record.schema_version} is not the supported {SCHEMA_VERSION}")
    if state["environment"] != pipeline.environment.value:
        raise CheckpointError(f"checkpoint is for {state['environment']}, this is {pipeline.environment.value}")
    execution, broker = pipeline.execution, pipeline.execution.broker
    if not hasattr(broker, "restore_state"):
        raise CheckpointError("only a simulated broker can be restored from a checkpoint")
    try:
        pipeline.ledger.restore_state(state["ledger"])
        broker.restore_state(state["broker"])
        pipeline.sizing.restore_state(state["sizing"])
        execution.restore_state(state["execution"])
        pipeline.restore_registry(state["registry"])
    except (KeyError, ValueError) as e:
        raise CheckpointError(f"checkpoint {record.id} is damaged: {type(e).__name__}: {e}") from None

    lost: list[str] = []

    def truth(client_order_id: str) -> Optional[OrderState]:
        try:
            return broker.get_order_status(client_order_id).state
        except KeyError:
            lost.append(client_order_id)
            return None
    execution.recover_from_store(truth)

    saved_risk = RiskState(state["risk_state"])
    if saved_risk is not RiskState.NORMAL:                       # a halt survives a restart
        pipeline.risk_controller.set_state(saved_risk, reason=f"restored from checkpoint {record.id}")

    report = pipeline.reconciliation.reconcile()
    if not report.consistent:
        raise CheckpointError("the restored ledger does not reconcile with the restored broker: "
                              + "; ".join(f"{m.kind} ({m.detail})" for m in report.mismatches))
    unprotected = []
    for symbol in pipeline.ledger.positions:
        entry = pipeline.open_stops.get(symbol)
        try:
            live = entry is not None and broker.get_order_status(entry[0]).state in _LIVE_STOPS
        except KeyError:
            live = False
        if not live:
            unprotected.append(symbol)
    if unprotected:
        raise CheckpointError("held position(s) without a live protective stop after restore: "
                              + ", ".join(sorted(unprotected)))
    interrupted = None
    if record.reason.startswith("session_start:"):
        interrupted = date.fromisoformat(record.reason.split(":", 1)[1])
    return RestoreReport(checkpoint_id=record.id, checkpoint_at=record.at.isoformat(), reason=record.reason,
                         lost_orders=sorted(lost), interrupted_session=interrupted)


def restore_latest(pipeline: TradingPipeline, store: DurableCheckpointStore) -> RestoreReport:
    record = store.latest_valid()
    if record is None:
        raise CheckpointError("no valid checkpoint to resume from"
                              + (f" ({store.skipped_corrupt} damaged)" if store.skipped_corrupt else ""))
    return restore(pipeline, record)
