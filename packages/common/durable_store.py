"""Durable persistence (docs/SAFETY_AUDIT.md F9) — the one SQLite file this
repo has, one table per narrowly-scoped consumer:

- `DurableOrderStore` (F3): idempotency that survives a process restart —
  which `client_order_id`s have already been submitted to a broker, and
  their last known state. The durable half of `ExecutionEngine`'s own
  idempotency check (`packages.broker_adapters.base.
  DuplicateClientOrderIdError`).
- `DurableAuditStore` (F7): the pre-trade audit trail
  (`services.pdca.audit_log.PreTradeRecord`) for every order that reached
  `ExecutionEngine.submit()`, so "why did this order reach the broker"
  (A5) survives a crash, not just the lifetime of the process.

Neither is a general persistence layer — `docs/database.md`'s full 15-table
V1 schema (decision provenance, fills, ledger, etc.) remains future work.
`docs/database.md` already names SQLite as the V1 fallback
("インメモリ/SQLite互換のリポジトリ層"); this uses stdlib `sqlite3`, so no
new dependency.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from packages.broker_adapters.base import DuplicateClientOrderIdError
from packages.common.environment import Environment
from packages.schemas.audit import ApprovedOrderSnapshot
from packages.schemas.core import OrderState


@dataclass(frozen=True)
class DurableOrderRecord:
    client_order_id: str
    decision_id: str
    risk_approval_id: str
    snapshot: ApprovedOrderSnapshot
    state: OrderState
    environment: Environment
    created_at: datetime
    updated_at: datetime


class DurableOrderStore:
    """One row per `client_order_id`. Table name is namespaced by
    `Environment` (§73) so PAPER and LIVE data can never share a row even if
    they are pointed at the same file — mirrors
    `packages.common.environment.require_same_environment`'s
    construction-time separation elsewhere in this codebase."""

    def __init__(self, path: str, environment: Environment) -> None:
        self.environment = environment
        self._table = environment.namespace("orders")
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "client_order_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, "
            "risk_approval_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, "
            "state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self._conn.commit()

    def record_submission(self, client_order_id: str, decision_id: str,
                          risk_approval_id: str, snapshot: ApprovedOrderSnapshot,
                          state: OrderState, at: datetime) -> None:
        """INSERT-only (§49 Event Sourcing intent): a second call for the
        same `client_order_id` — including across a restart, since this is
        durable — is the exact duplicate INV-7 exists to catch."""
        try:
            self._conn.execute(
                f"INSERT INTO {self._table} "
                "(client_order_id, decision_id, risk_approval_id, snapshot_json, "
                "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (client_order_id, decision_id, risk_approval_id,
                 snapshot.model_dump_json(), state.value, at.isoformat(), at.isoformat()))
            self._conn.commit()
        except sqlite3.IntegrityError as e:
            raise DuplicateClientOrderIdError(client_order_id) from e

    def update_state(self, client_order_id: str, state: OrderState, at: datetime) -> None:
        cur = self._conn.execute(
            f"UPDATE {self._table} SET state = ?, updated_at = ? WHERE client_order_id = ?",
            (state.value, at.isoformat(), client_order_id))
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(client_order_id)

    def is_known(self, client_order_id: str) -> bool:
        cur = self._conn.execute(
            f"SELECT 1 FROM {self._table} WHERE client_order_id = ?", (client_order_id,))
        return cur.fetchone() is not None

    def get(self, client_order_id: str) -> Optional[DurableOrderRecord]:
        cur = self._conn.execute(
            f"SELECT client_order_id, decision_id, risk_approval_id, snapshot_json, "
            f"state, created_at, updated_at FROM {self._table} WHERE client_order_id = ?",
            (client_order_id,))
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def all(self) -> list[DurableOrderRecord]:
        """Every durably known order, for startup recovery (F3) — the caller
        is expected to reconcile against the broker before trusting this."""
        cur = self._conn.execute(
            f"SELECT client_order_id, decision_id, risk_approval_id, snapshot_json, "
            f"state, created_at, updated_at FROM {self._table}")
        return [self._row_to_record(row) for row in cur.fetchall()]

    def _row_to_record(self, row: tuple) -> DurableOrderRecord:
        cid, decision_id, risk_approval_id, snapshot_json, state, created_at, updated_at = row
        return DurableOrderRecord(
            client_order_id=cid, decision_id=decision_id, risk_approval_id=risk_approval_id,
            snapshot=ApprovedOrderSnapshot.model_validate_json(snapshot_json),
            state=OrderState(state), environment=self.environment,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at))

    def close(self) -> None:
        self._conn.close()


@dataclass(frozen=True)
class DurableAuditRecord:
    client_order_id: str
    decision_id: str
    record_json: str
    environment: Environment
    created_at: datetime
    updated_at: datetime


class DurableAuditStore:
    """docs/SAFETY_AUDIT.md F7: one row per `client_order_id` that reached
    `ExecutionEngine.submit()`, storing a full JSON snapshot of its
    `PreTradeRecord` (`services.pdca.audit_log`). Scope matches A5's own
    definition of traceability — "an order that reached the broker must
    carry the full chain" (`PreTradeAuditLog.is_fully_traceable`) — so only
    submitted orders are persisted here. Rejections that never reach
    execution (audit REJECT/REVIEW, risk REJECT) stay in-memory-only
    (`PreTradeAuditLog.near_misses`); losing that A6 "prevented near-misses
    this month" stat to a crash is a real but smaller gap than losing A5
    traceability for an order that actually reached the broker, and is not
    fixed here.

    `upsert`, not insert-only: unlike an order's identity (F3), a
    `PreTradeRecord`'s fields (fills, final_state) are appended/refined
    after the row is first written, and the whole point is capturing the
    latest known snapshot, not the first one."""

    def __init__(self, path: str, environment: Environment) -> None:
        self.environment = environment
        self._table = environment.namespace("pre_trade_audit")
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "client_order_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, "
            "record_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self._conn.commit()

    def upsert(self, client_order_id: str, decision_id: str, record_json: str,
              at: datetime) -> None:
        self._conn.execute(
            f"INSERT INTO {self._table} "
            "(client_order_id, decision_id, record_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(client_order_id) DO UPDATE SET "
            "record_json = excluded.record_json, updated_at = excluded.updated_at",
            (client_order_id, decision_id, record_json, at.isoformat(), at.isoformat()))
        self._conn.commit()

    def get(self, client_order_id: str) -> Optional[DurableAuditRecord]:
        cur = self._conn.execute(
            f"SELECT client_order_id, decision_id, record_json, created_at, updated_at "
            f"FROM {self._table} WHERE client_order_id = ?", (client_order_id,))
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def all(self) -> list[DurableAuditRecord]:
        cur = self._conn.execute(
            f"SELECT client_order_id, decision_id, record_json, created_at, updated_at "
            f"FROM {self._table}")
        return [self._row_to_record(row) for row in cur.fetchall()]

    def _row_to_record(self, row: tuple) -> DurableAuditRecord:
        cid, decision_id, record_json, created_at, updated_at = row
        return DurableAuditRecord(
            client_order_id=cid, decision_id=decision_id, record_json=record_json,
            environment=self.environment,
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at))

    def close(self) -> None:
        self._conn.close()
