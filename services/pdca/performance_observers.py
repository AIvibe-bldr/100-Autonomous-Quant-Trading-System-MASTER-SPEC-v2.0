"""Performance learning loops (MASTER SPEC §50-55), as read-only session observers.

- AttributionObserver  : realized P&L of closed trades split into market beta / alpha / slippage /
                         fees, then scored for where the profit came from (§54-55).
- ExitReviewObserver   : grades stops after the fact, tracks NO-TRADE decisions as counterfactuals
                         (§50-51, §53), and — only when the evidence is strong — files a RESEARCH
                         proposal with the Exit Optimizer (§52). A proposal never changes live exits.
- DailyReviewObserver  : the §63-64 daily review (grades and WHY PROFIT / WHY LOSS / WHY NO TRADE).

None of them can touch risk, configuration or orders (see services/pdca/observer.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from packages.common.experiments import ExperimentRegistry
from packages.schemas.core import Action, StopType
from services.pdca.attribution import TradeRecord, attribute, profit_quality
from services.pdca.observer import DecisionEvent, FillEvent, SessionEndEvent, SessionObserver
from services.pdca.post_trade import PostTradeTracker
from services.pdca.exit_optimizer import ExitOptimizer
from services.pdca.review import DailyReview

BENCHMARK_SYMBOL = "SPY"
MIN_GRADED_STOPS_FOR_PROPOSAL = 20
EARLY_STOP_RATE_FOR_PROPOSAL = 0.5


@dataclass
class _Lot:
    qty: float = 0.0
    avg_cost: float = 0.0
    entry_at: Optional[datetime] = None
    fees: float = 0.0
    slippage: float = 0.0


@dataclass
class _Closed:
    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    entry_at: datetime
    exit_at: datetime
    fees: float
    slippage: float


class AttributionObserver(SessionObserver):
    """Beta is assumed 1.0 against the benchmark (a simplification, stated in the summary): the
    'alpha' is whatever the benchmark's move over the holding period does not explain."""
    name = "attribution"

    def __init__(self, benchmark: str = BENCHMARK_SYMBOL) -> None:
        self._benchmark = benchmark
        self._lots: dict[str, _Lot] = {}
        self._closed: list[_Closed] = []
        self.attributions = []
        self.unattributed = 0

    def on_fill(self, e: FillEvent) -> None:
        lot = self._lots.setdefault(e.symbol, _Lot())
        if e.side is Action.BUY:
            total = lot.qty + e.qty
            lot.avg_cost = (lot.avg_cost * lot.qty + e.price * e.qty) / total
            lot.qty = total
            lot.entry_at = lot.entry_at or e.ts           # the holding period starts at the first buy
            lot.fees += e.fees
            if e.reference_price is not None:
                lot.slippage += max(0.0, e.price - e.reference_price) * e.qty
            return
        if lot.qty <= 1e-9 or lot.entry_at is None:
            return                                         # an exit we never saw the entry of
        qty = min(e.qty, lot.qty)
        share = qty / lot.qty
        exit_slip = max(0.0, e.reference_price - e.price) * qty if e.reference_price is not None else 0.0
        self._closed.append(_Closed(
            symbol=e.symbol, qty=qty, entry_price=lot.avg_cost, exit_price=e.price,
            entry_at=lot.entry_at, exit_at=e.ts,
            fees=lot.fees * share + e.fees * (qty / e.qty), slippage=lot.slippage * share + exit_slip))
        lot.fees -= lot.fees * share
        lot.slippage -= lot.slippage * share
        lot.qty -= qty
        if lot.qty <= 1e-9:
            del self._lots[e.symbol]

    def on_session_end(self, e: SessionEndEvent) -> None:
        for c in self._closed:
            start, end = e.price_on(self._benchmark, c.entry_at), e.price_on(self._benchmark, c.exit_at)
            if not start or not end:
                self.unattributed += 1                    # no benchmark: counted, not guessed
                continue
            self.attributions.append(attribute(TradeRecord(
                symbol=c.symbol, qty=c.qty, entry_price=c.entry_price, exit_price=c.exit_price,
                market_return_during_hold=end / start - 1.0, slippage_cost=c.slippage, fees=c.fees,
                alpha_name="ai-decision")))
        self._closed.clear()

    def summary(self) -> dict[str, Any]:
        a = self.attributions
        out: dict[str, Any] = {
            "closed_trades": len(a), "unattributed": self.unattributed, "benchmark": self._benchmark,
            "assumption": "market beta = 1.0 against the benchmark over the holding period",
            "total_pnl": sum(x.total_pnl for x in a), "market_beta_pnl": sum(x.market_beta_pnl for x in a),
            "alpha_pnl": sum(x.alpha_pnl for x in a), "slippage": sum(x.slippage for x in a),
            "fees": sum(x.fees for x in a)}
        if a:
            q = profit_quality(a)
            out["profit_quality"] = {"source": q.source.value, "score": round(q.score, 3),
                                     "detail": q.detail}
        return out


class ExitReviewObserver(SessionObserver):
    name = "exit_review"

    def __init__(self, tracker: PostTradeTracker, registry: Optional[ExperimentRegistry] = None) -> None:
        self.tracker = tracker                            # the pipeline's own tracker (stop exits)
        self.optimizer = ExitOptimizer(registry or ExperimentRegistry())

    def on_decision(self, e: DecisionEvent) -> None:
        if e.decision.action.value != "BUY" and e.price is not None:
            self.tracker.track(e.symbol, e.price, e.as_of, kind="COUNTERFACTUAL")      # §53

    def on_session_end(self, e: SessionEndEvent) -> None:
        self.tracker.evaluate(e.as_of, e.price_on)
        stats = self.optimizer.analyze_stops(self.tracker)
        if (stats.get("graded", 0) >= MIN_GRADED_STOPS_FOR_PROPOSAL
                and stats["early_rate"] >= EARLY_STOP_RATE_FOR_PROPOSAL
                and not any(p.name == "review-early-stops" for p in self.optimizer.proposals)):
            self.optimizer.propose(
                "review-early-stops", StopType.ATR,
                f"{stats['early_rate']:.0%} of {int(stats['graded'])} graded stops were early "
                f"(price recovered >=3% within a week): research a wider or time-aware stop",
                e.as_of)

    def summary(self) -> dict[str, Any]:
        cf = [t for t in self.tracker.tracked if t.kind == "COUNTERFACTUAL" and "1w" in t.later_prices]
        rets = [t.later_prices["1w"] / t.exit_price - 1.0 for t in cf]
        return {
            "stops": self.optimizer.analyze_stops(self.tracker),
            "counterfactual": {"tracked": sum(t.kind == "COUNTERFACTUAL" for t in self.tracker.tracked),
                               "with_1w_outcome": len(cf),
                               "mean_1w_return_of_skipped": (sum(rets) / len(rets)) if rets else None,
                               "note": "NO_TRADE/WAIT/AVOID decisions only; BUYs that a later gate "
                                       "stopped are not tracked"},
            "proposals": [{"name": p.name, "stage": p.stage.value, "description": p.change_description}
                          for p in self.optimizer.proposals]}


class DailyReviewObserver(SessionObserver):
    name = "daily_review"

    def __init__(self, initial_equity: float, previous_equity: Optional[float] = None) -> None:
        self._initial = initial_equity
        self._prev = previous_equity if previous_equity is not None else initial_equity   # resume: last session's equity
        self._day = 0
        self._last: Optional[dict[str, Any]] = None

    def on_session_end(self, e: SessionEndEvent) -> None:
        self._day += 1
        review = DailyReview(day=e.as_of.date(), pnl=e.equity - self._prev, equity=e.equity,
                             start_equity=self._initial, day_index=self._day, result=e.result,
                             risk_violations=0, data_issues=len(e.result.data_warnings))
        grades = review.compute_grades()
        self._last = {"day": review.day.isoformat(), "pnl": round(review.pnl, 4), "grades": grades,
                      "narrative": review.narrative()}
        self._prev = e.equity

    def summary(self) -> dict[str, Any]:
        return self._last or {}
