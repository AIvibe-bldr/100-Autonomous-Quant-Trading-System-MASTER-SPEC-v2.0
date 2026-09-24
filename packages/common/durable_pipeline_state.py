"""Durable Pipeline State (docs/SAFETY_AUDIT.md F9 follow-up) — persists
Ledger / DecisionQualityEngine / the equity-chart series across a process
restart.

Narrowly scoped to what `scripts/run_live_dashboard.py` needs to survive
being stopped and restarted (cash, positions, decision history, the equity
chart) — not a general repository layer for every consumer in the system;
`docs/database.md`'s full 15-table V1 schema remains separate, future work.
Same stdlib-`sqlite3`, one-table-per-consumer, `Environment`-namespaced
pattern as `packages.common.durable_store` (so PAPER/LIVE state can never
mix even pointed at the same file).

Checkpointed as a full-state snapshot after each session (`save()` deletes
and re-inserts every table in one transaction), not an incrementally
replayed event log: `Ledger`'s cost-basis math (avg_cost, realized P&L) is
owned by `Ledger.record_fill` alone, and replaying raw entries through a
second, independent implementation here would be exactly the kind of
duplicated business rule §6 of CLAUDE.md warns against. `Ledger.restore()`
takes the already-computed final state directly instead.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional

from packages.common.clock import ensure_utc
from packages.common.environment import Environment
from packages.common.ledger import EntryKind, Ledger, LedgerEntry, PositionLot
from services.pdca.decision_quality import DecisionQualityEngine, DecisionSnapshot


class DurablePipelineState:
    def __init__(self, path: str, environment: Environment) -> None:
        self.environment = environment
        self._t_meta = environment.namespace("pipeline_meta")
        self._t_positions = environment.namespace("pipeline_positions")
        self._t_entries = environment.namespace("pipeline_entries")
        self._t_snapshots = environment.namespace("pipeline_decision_snapshots")
        self._t_equity = environment.namespace("pipeline_equity_series")
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t_meta} ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), initial_cash REAL NOT NULL, "
            "cash REAL NOT NULL, realized_pnl REAL NOT NULL, fees_paid REAL NOT NULL, "
            "high_water_mark REAL NOT NULL, currency TEXT NOT NULL, "
            "last_session_at TEXT, updated_at TEXT NOT NULL)")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t_positions} ("
            "symbol TEXT PRIMARY KEY, qty REAL NOT NULL, avg_cost REAL NOT NULL)")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t_entries} ("
            "entry_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, at TEXT NOT NULL, "
            "kind TEXT NOT NULL, amount REAL NOT NULL, symbol TEXT, qty REAL NOT NULL, "
            "price REAL NOT NULL, realized_pnl REAL NOT NULL, note TEXT NOT NULL)")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t_snapshots} ("
            "decision_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t_equity} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, equity REAL NOT NULL)")
        self._conn.commit()

    # -- save ------------------------------------------------------------
    def save(self, ledger: Ledger, decision_quality: DecisionQualityEngine,
             equity_series: list[dict[str, Any]],
             last_session_at: Optional[datetime]) -> None:
        """Full-state checkpoint, one transaction. Called after each
        session (not on every individual mutation) — a Mock-data session
        completes in well under a second, so re-writing the whole picture
        every time is cheap, and it avoids ever needing to reconcile a
        partial incremental write against a crash mid-session."""
        now = datetime.now().isoformat()
        self._conn.execute(f"DELETE FROM {self._t_meta}")
        self._conn.execute(
            f"INSERT INTO {self._t_meta} (id, initial_cash, cash, realized_pnl, "
            "fees_paid, high_water_mark, currency, last_session_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ledger.initial_cash, ledger.cash, ledger.realized_pnl, ledger.fees_paid,
             ledger.high_water_mark, ledger.currency,
             last_session_at.isoformat() if last_session_at else None, now))

        self._conn.execute(f"DELETE FROM {self._t_positions}")
        self._conn.executemany(
            f"INSERT INTO {self._t_positions} (symbol, qty, avg_cost) VALUES (?, ?, ?)",
            [(s, lot.qty, lot.avg_cost) for s, lot in ledger.positions.items()])

        self._conn.execute(f"DELETE FROM {self._t_entries}")
        self._conn.executemany(
            f"INSERT INTO {self._t_entries} (entry_id, seq, at, kind, amount, symbol, "
            "qty, price, realized_pnl, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(e.entry_id, i, e.at.isoformat(), e.kind.value, e.amount, e.symbol,
              e.qty, e.price, e.realized_pnl, e.note)
             for i, e in enumerate(ledger.entries)])

        self._conn.execute(f"DELETE FROM {self._t_snapshots}")
        self._conn.executemany(
            f"INSERT INTO {self._t_snapshots} (decision_id, payload_json) VALUES (?, ?)",
            [(s.decision_id, s.model_dump_json()) for s in decision_quality.all_snapshots()])

        self._conn.execute(f"DELETE FROM {self._t_equity}")
        self._conn.executemany(
            f"INSERT INTO {self._t_equity} (at, equity) VALUES (?, ?)",
            [(row["at"], row["equity"]) for row in equity_series])
        self._conn.commit()

    # -- load ------------------------------------------------------------
    def has_saved_state(self) -> bool:
        return self._conn.execute(
            f"SELECT 1 FROM {self._t_meta} WHERE id = 1").fetchone() is not None

    def load_ledger(self) -> Ledger:
        meta = self._conn.execute(
            f"SELECT initial_cash, cash, realized_pnl, fees_paid, high_water_mark, "
            f"currency FROM {self._t_meta} WHERE id = 1").fetchone()
        if meta is None:
            raise ValueError("no saved pipeline state — call has_saved_state() first")
        initial_cash, cash, realized_pnl, fees_paid, high_water_mark, currency = meta

        positions = {
            symbol: PositionLot(symbol=symbol, qty=qty, avg_cost=avg_cost)
            for symbol, qty, avg_cost in
            self._conn.execute(f"SELECT symbol, qty, avg_cost FROM {self._t_positions}")}

        entries = [
            LedgerEntry(at=ensure_utc(datetime.fromisoformat(at)), kind=EntryKind(kind),
                       amount=amount, symbol=symbol, qty=qty, price=price,
                       realized_pnl=r_pnl, note=note, entry_id=entry_id)
            for entry_id, at, kind, amount, symbol, qty, price, r_pnl, note in
            self._conn.execute(
                f"SELECT entry_id, at, kind, amount, symbol, qty, price, realized_pnl, "
                f"note FROM {self._t_entries} ORDER BY seq")]

        return Ledger.restore(initial_cash=initial_cash, cash=cash, positions=positions,
                              realized_pnl=realized_pnl, fees_paid=fees_paid,
                              high_water_mark=high_water_mark, entries=entries,
                              currency=currency)

    def load_decision_snapshots(self) -> list[DecisionSnapshot]:
        return [DecisionSnapshot.model_validate_json(payload) for (payload,) in
                self._conn.execute(f"SELECT payload_json FROM {self._t_snapshots}")]

    def load_equity_series(self) -> list[dict[str, Any]]:
        return [{"at": at, "equity": equity} for at, equity in
                self._conn.execute(f"SELECT at, equity FROM {self._t_equity} ORDER BY id")]

    def load_last_session_at(self) -> Optional[datetime]:
        row = self._conn.execute(
            f"SELECT last_session_at FROM {self._t_meta} WHERE id = 1").fetchone()
        if row is None or row[0] is None:
            return None
        return ensure_utc(datetime.fromisoformat(row[0]))
