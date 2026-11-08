"""Records one row per trading session (multi-day paper results, docs/paper_live_gate.md §3).

A callable for `SessionRunner(after_session=...)`. Read-only with respect to the pipeline: it
takes a snapshot of what the session left behind and writes it to the session store — it
changes no trading state. Equity is marked at session time (after the session's trades), so
`day_pnl` is an open-to-open figure, not a close-to-close one."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from packages.common.calendar import ET
from packages.common.clock import ensure_utc
from packages.common.durable_store import DurableSessionStore, SessionRecord
from services.pipeline import PipelineResult, TradingPipeline

_MAX_DETAIL_ITEMS = 10


class SessionRecorder:
    def __init__(self, pipeline: TradingPipeline, store: DurableSessionStore, model_tag: str) -> None:
        if not model_tag.strip():
            raise ValueError("a model_tag is needed so runs can be compared")
        self._pipeline = pipeline
        self._store = store
        self._tag = model_tag.strip()

    def __call__(self, at: datetime, result: PipelineResult,
                 llm_cost_usd: Optional[float] = None) -> SessionRecord:
        p = self._pipeline
        snap = p.ledger.snapshot(p.mark_prices({}, at))
        previous = self._store.latest_equity()
        baseline = p.ledger.initial_cash if previous is None else previous
        detail = {
            "no_trade_reasons": result.no_trade_reasons[:_MAX_DETAIL_ITEMS],
            "data_warnings": result.data_warnings[:_MAX_DETAIL_ITEMS],
            "fills": [{"symbol": f.symbol, "side": f.side.value, "qty": f.qty, "price": f.price}
                      for f in result.fills[:_MAX_DETAIL_ITEMS]],
            "disagreement_multipliers": result.disagreement_multipliers,
            # cumulative state of the pipeline's observers (forecast calibration, ...)
            "observers": {o.name: o.summary() for o in p.observers},
        }
        rec = SessionRecord(
            session_date=ensure_utc(at).astimezone(ET).date().isoformat(), model_tag=self._tag,
            recorded_at=ensure_utc(at), equity=snap.equity, cash=snap.cash,
            high_water_mark=snap.high_water_mark, drawdown=snap.drawdown,
            day_pnl=snap.equity - baseline, realized_pnl_total=p.ledger.realized_pnl,
            fees_total=p.ledger.fees_paid, scanned=result.scanned, candidates=result.candidates,
            decision_candidates=result.decision_candidates, skeptic_vetoes=result.skeptic_vetoes,
            audit_passed=result.audit_passed, risk_passed=result.risk_passed,
            orders_filled=result.orders_filled, stops_triggered=result.stops_triggered,
            open_positions=sum(1 for lot in p.ledger.positions.values() if lot.qty > 0),
            data_warnings=len(result.data_warnings), risk_state=p.risk_controller.state.value,
            llm_cost_usd=llm_cost_usd, detail_json=json.dumps(detail, default=str))
        self._store.record(rec)
        return rec
