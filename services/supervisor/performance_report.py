"""Summaries of recorded sessions, per run, for side-by-side comparison.

Deliberately plain: totals, drawdown and counts — no Sharpe, no annualisation, no ranking.
With a few dozen daily observations those numbers look precise and mean little, so every
summary carries its sample size and the report says plainly when it is too small to judge."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from packages.common.durable_store import SessionRecord

# docs/paper_live_gate.md §3 (proposal, not yet approved): consecutive sessions wanted
# before any conclusion about a configuration is drawn
PROPOSED_MIN_SESSIONS = 60


@dataclass(frozen=True)
class RunSummary:
    model_tag: str
    sessions: int
    first_date: str
    last_date: str
    start_equity: float          # equity before the first recorded session
    end_equity: float
    total_return: float
    max_drawdown: float          # worst recorded drawdown from the high-water mark
    up_days: int
    down_days: int
    flat_days: int
    best_day: float
    worst_day: float
    orders_filled: int
    stops_triggered: int
    fees_total: float
    non_normal_risk_days: int    # sessions that ended in a risk state other than NORMAL
    warning_days: int
    llm_cost_usd: Optional[float]   # None when no session recorded a cost

    @property
    def enough_sessions(self) -> bool:
        return self.sessions >= PROPOSED_MIN_SESSIONS


def summarize(records: Sequence[SessionRecord]) -> RunSummary:
    if not records:
        raise ValueError("no sessions recorded")
    recs = sorted(records, key=lambda r: r.session_date)
    start = recs[0].equity - recs[0].day_pnl
    pnls = [r.day_pnl for r in recs]
    costs = [r.llm_cost_usd for r in recs if r.llm_cost_usd is not None]
    return RunSummary(
        model_tag=recs[0].model_tag if len({r.model_tag for r in recs}) == 1 else "MIXED",
        sessions=len(recs), first_date=recs[0].session_date, last_date=recs[-1].session_date,
        start_equity=start, end_equity=recs[-1].equity,
        total_return=(recs[-1].equity / start - 1.0) if start > 0 else 0.0,
        max_drawdown=max(r.drawdown for r in recs),
        up_days=sum(p > 1e-9 for p in pnls), down_days=sum(p < -1e-9 for p in pnls),
        flat_days=sum(abs(p) <= 1e-9 for p in pnls), best_day=max(pnls), worst_day=min(pnls),
        orders_filled=sum(r.orders_filled for r in recs),
        stops_triggered=sum(r.stops_triggered for r in recs), fees_total=recs[-1].fees_total,
        non_normal_risk_days=sum(r.risk_state != "NORMAL" for r in recs),
        warning_days=sum(r.data_warnings > 0 for r in recs),
        llm_cost_usd=sum(costs) if costs else None)


def render(summaries: Sequence[RunSummary]) -> str:
    lines = []
    for s in summaries:
        cost = "n/a" if s.llm_cost_usd is None else f"${s.llm_cost_usd:.2f}"
        lines += [
            f"[{s.model_tag}] {s.first_date} .. {s.last_date}  ({s.sessions} sessions)",
            f"  equity ${s.start_equity:,.2f} -> ${s.end_equity:,.2f}  return {s.total_return:+.2%}"
            f"  max drawdown {s.max_drawdown:.2%}",
            f"  days up/down/flat {s.up_days}/{s.down_days}/{s.flat_days}"
            f"  best {s.best_day:+.2f}  worst {s.worst_day:+.2f}",
            f"  fills {s.orders_filled}  stops {s.stops_triggered}  fees ${s.fees_total:.2f}"
            f"  LLM cost {cost}",
            f"  sessions ending non-NORMAL {s.non_normal_risk_days}  with data warnings {s.warning_days}",
        ]
        if not s.enough_sessions:
            lines.append(f"  !! {s.sessions}/{PROPOSED_MIN_SESSIONS} sessions: too few to say anything "
                         f"about profitability or to rank this against another run")
    if len(summaries) > 1:
        lines.append("Runs differ in more than the AI stack (market days, data, timing, luck); "
                     "a difference here is not evidence that one is better.")
    return "\n".join(lines)
