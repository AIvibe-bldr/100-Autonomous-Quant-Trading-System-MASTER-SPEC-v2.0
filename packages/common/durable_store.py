"""Durable Order Store (docs/SAFETY_AUDIT.md F9/F3) — idempotency that
survives a process restart.

Scope is deliberately narrow: this backs the ONE fact that must never be
lost to a crash — which `client_order_id`s have already been submitted to a
broker, and their last known state. It is the durable half of
`ExecutionEngine`'s own idempotency check
(`packages.broker_adapters.base.DuplicateClientOrderIdError`), not a general
persistence layer. `docs/database.md` already names SQLite as the V1
fallback ("インメモリ/SQLite互換のリポジトリ層"); this uses stdlib `sqlite3`,
so no new dependency. A durable audit trail (F7) and decision provenance are
future extensions of the same store, not built here.
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
