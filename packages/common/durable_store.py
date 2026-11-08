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

- `DurablePitStore` (§10): the durable twin of `PointInTimeStore` — every record keeps the
  time we RECEIVED it, so a query "as of T" can never see what arrived after T.
- `DurableSessionStore`: one row per trading session (equity, drawdown, funnel counts, risk
  state, warnings) so multi-day paper results can be measured and compared across runs.

None is a general persistence layer — `docs/database.md`'s full 15-table
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


@dataclass(frozen=True)
class SessionRecord:
    """What one trading session left behind. `day_pnl` is the change in equity from the
    previous recorded session (or from the initial cash for the first), measured at session
    time — not at the close — so it is an open-to-open figure."""
    session_date: str            # trading date in ET, ISO
    model_tag: str               # which AI stack / configuration produced it (for comparison)
    recorded_at: datetime
    equity: float
    cash: float
    high_water_mark: float
    drawdown: float
    day_pnl: float
    realized_pnl_total: float
    fees_total: float
    scanned: int
    candidates: int
    decision_candidates: int
    skeptic_vetoes: int
    audit_passed: int
    risk_passed: int
    orders_filled: int
    stops_triggered: int
    open_positions: int
    data_warnings: int
    risk_state: str
    llm_cost_usd: Optional[float]
    detail_json: str             # no-trade reasons, warnings, fills


_SESSION_COLUMNS = (
    "session_date", "model_tag", "recorded_at", "equity", "cash", "high_water_mark", "drawdown",
    "day_pnl", "realized_pnl_total", "fees_total", "scanned", "candidates", "decision_candidates",
    "skeptic_vetoes", "audit_passed", "risk_passed", "orders_filled", "stops_triggered",
    "open_positions", "data_warnings", "risk_state", "llm_cost_usd", "detail_json")


class DurableSessionStore:
    """Append-only: a session date is recorded once. A second record for the same date is an
    error (it would overwrite history), not an upsert."""

    def __init__(self, path: str, environment: Environment) -> None:
        self.environment = environment
        self._table = environment.namespace("sessions")
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "session_date TEXT PRIMARY KEY, model_tag TEXT NOT NULL, recorded_at TEXT NOT NULL, "
            "equity REAL NOT NULL, cash REAL NOT NULL, high_water_mark REAL NOT NULL, "
            "drawdown REAL NOT NULL, day_pnl REAL NOT NULL, realized_pnl_total REAL NOT NULL, "
            "fees_total REAL NOT NULL, scanned INTEGER NOT NULL, candidates INTEGER NOT NULL, "
            "decision_candidates INTEGER NOT NULL, skeptic_vetoes INTEGER NOT NULL, "
            "audit_passed INTEGER NOT NULL, risk_passed INTEGER NOT NULL, "
            "orders_filled INTEGER NOT NULL, stops_triggered INTEGER NOT NULL, "
            "open_positions INTEGER NOT NULL, data_warnings INTEGER NOT NULL, "
            "risk_state TEXT NOT NULL, llm_cost_usd REAL, detail_json TEXT NOT NULL)")
        self._conn.commit()

    def record(self, rec: SessionRecord) -> None:
        values = [getattr(rec, c) for c in _SESSION_COLUMNS]
        values[2] = rec.recorded_at.isoformat()
        try:
            self._conn.execute(
                f"INSERT INTO {self._table} ({', '.join(_SESSION_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_SESSION_COLUMNS))})", values)
        except sqlite3.IntegrityError:
            raise ValueError(f"session {rec.session_date} is already recorded") from None
        self._conn.commit()

    def all(self) -> list[SessionRecord]:
        cur = self._conn.execute(
            f"SELECT {', '.join(_SESSION_COLUMNS)} FROM {self._table} ORDER BY session_date")
        out = []
        for row in cur.fetchall():
            d = dict(zip(_SESSION_COLUMNS, row))
            d["recorded_at"] = datetime.fromisoformat(d["recorded_at"])
            out.append(SessionRecord(**d))
        return out

    def latest_equity(self) -> Optional[float]:
        cur = self._conn.execute(
            f"SELECT equity FROM {self._table} ORDER BY session_date DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()


@dataclass(frozen=True)
class DurablePitRecord:
    key: str
    value: object
    event_time: datetime
    received_time: datetime
    source: str


class DurablePitStore:
    """Point-in-Time records (MASTER SPEC §10) in SQLite. Same contract as
    `packages.common.pit_store.PointInTimeStore` (`put`, `get_as_of`, `history`), plus
    durability: append-only, values are JSON. Among records received at the same instant the
    later `put` wins, exactly as in the in-memory store."""

    def __init__(self, path: str, environment: Environment) -> None:
        self.environment = environment
        self._table = environment.namespace("pit")
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value_json TEXT NOT NULL, "
            "event_time TEXT NOT NULL, received_time TEXT NOT NULL, source TEXT NOT NULL)")
        self._conn.execute(
            f"CREATE INDEX IF NOT EXISTS {self._table}_key ON {self._table} (key, received_time)")
        self._conn.commit()

    def put(self, key: str, value: object, event_time: datetime, received_time: datetime,
            source: str = "unknown") -> None:
        import json

        from packages.common.clock import ensure_utc
        self._conn.execute(
            f"INSERT INTO {self._table} (key, value_json, event_time, received_time, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, json.dumps(value, default=str), ensure_utc(event_time).isoformat(),
             ensure_utc(received_time).isoformat(), source))
        self._conn.commit()

    def get_as_of(self, key: str, as_of: datetime) -> Optional[DurablePitRecord]:
        from packages.common.clock import ensure_utc
        cur = self._conn.execute(
            f"SELECT key, value_json, event_time, received_time, source FROM {self._table} "
            "WHERE key = ? AND received_time <= ? ORDER BY received_time DESC, id DESC LIMIT 1",
            (key, ensure_utc(as_of).isoformat()))
        row = cur.fetchone()
        return self._row(row) if row else None

    def history(self, key: str) -> list[DurablePitRecord]:
        cur = self._conn.execute(
            f"SELECT key, value_json, event_time, received_time, source FROM {self._table} "
            "WHERE key = ? ORDER BY received_time, id", (key,))
        return [self._row(r) for r in cur.fetchall()]

    @staticmethod
    def _row(row: tuple) -> DurablePitRecord:
        import json
        key, value_json, event_time, received_time, source = row
        return DurablePitRecord(key=key, value=json.loads(value_json),
                                event_time=datetime.fromisoformat(event_time),
                                received_time=datetime.fromisoformat(received_time), source=source)

    def close(self) -> None:
        self._conn.close()
