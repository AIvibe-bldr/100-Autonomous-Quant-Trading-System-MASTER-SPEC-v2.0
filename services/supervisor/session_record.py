"""Records one row per trading session (multi-day paper results, docs/paper_live_gate.md §3).

A callable for `SessionRunner(after_session=...)`. Read-only with respect to the pipeline: it
takes a snapshot of what the session left behind and writes it to the session store — it
changes no trading state. Equity is marked at session time (after the session's trades), so
`day_pnl` is an open-to-open figure, not a close-to-close one."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable, Optional

from packages.common.calendar import ET
from packages.common.clock import ensure_utc
from packages.common.durable_store import DurableSessionStore, SessionRecord
from packages.common.symbol_profiles import SymbolProfile, profile_of
from services.pipeline import PipelineResult, TradingPipeline

_MAX_DETAIL_ITEMS = 10
log = logging.getLogger("quant.session_record")
CompanyLookup = Callable[[str], Optional[SymbolProfile]]


def _auto_profile(symbol: str, lookup: Optional[CompanyLookup]) -> Optional[dict]:
    """Who a held ticker is, when the hand-written table does not know it. Best effort and display only: a failed
    lookup is logged and the holding is shown by its symbol — it never stops the session record."""
    if lookup is None or profile_of(symbol) is not None:
        return None
    try:
        prof = lookup(symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("company lookup for %s failed: %s: %s", symbol, type(e).__name__, e)
        return None
    return None if prof is None else {"company": prof.company, "category": prof.category, "business": prof.business}


def holdings_view(pipeline: TradingPipeline, prices: dict[str, float],
                  company_lookup: Optional[CompanyLookup] = None) -> list[dict]:
    """What is held at the end of the session, valued at `prices`, with the plan made before entry (§33).
    Plain data for the dashboard: it reads the books and the stop registry, and changes neither. The plan is the
    one stored with the protective stop — a position bought in several lots shows the latest lot's plan."""
    equity = pipeline.ledger.equity(prices)
    out = []
    for sym, lot in sorted(pipeline.ledger.positions.items()):
        if lot.qty <= 0:
            continue
        price = prices.get(sym, lot.avg_cost)
        cost, value = lot.qty * lot.avg_cost, lot.qty * price
        entry = pipeline.open_stops.get(sym)
        plan = None
        if entry is not None:
            stop = entry[1]
            plan = {"thesis": stop.thesis, "stop_price": stop.stop_price, "stop_type": stop.stop_type.value,
                    "stop_reason": stop.stop_reason, "profit_target": stop.profit_target,
                    "holding_horizon": stop.holding_horizon, "invalidation": stop.invalidation,
                    "risk_amount": entry[3]}
        out.append({"symbol": sym, "qty": lot.qty, "avg_cost": lot.avg_cost, "price": price, "value": value,
                    "pnl": value - cost, "pnl_pct": (value / cost - 1.0) if cost > 0 else 0.0,
                    "weight": value / equity if equity > 0 else 0.0, "plan": plan,
                    "profile_auto": _auto_profile(sym, company_lookup)})
    return out


class SessionRecorder:
    def __init__(self, pipeline: TradingPipeline, store: DurableSessionStore, model_tag: str,
                 company_lookup: Optional[CompanyLookup] = None) -> None:
        if not model_tag.strip():
            raise ValueError("a model_tag is needed so runs can be compared")
        self._pipeline = pipeline
        self._store = store
        self._tag = model_tag.strip()
        self._company_lookup = company_lookup

    def __call__(self, at: datetime, result: PipelineResult,
                 llm_cost_usd: Optional[float] = None) -> SessionRecord:
        p = self._pipeline
        prices = p.mark_prices({}, at)
        snap = p.ledger.snapshot(prices)
        previous = self._store.latest_equity()
        baseline = p.ledger.initial_cash if previous is None else previous
        detail = {
            "no_trade_reasons": result.no_trade_reasons[:_MAX_DETAIL_ITEMS],
            "data_warnings": result.data_warnings[:_MAX_DETAIL_ITEMS],
            "fills": [{"symbol": f.symbol, "side": f.side.value, "qty": f.qty, "price": f.price}
                      for f in result.fills[:_MAX_DETAIL_ITEMS]],
            "disagreement_multipliers": result.disagreement_multipliers,
            "holdings": holdings_view(p, prices, self._company_lookup),
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
