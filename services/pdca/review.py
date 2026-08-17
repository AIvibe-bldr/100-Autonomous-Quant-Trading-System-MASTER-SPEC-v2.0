"""Daily PDCA Review (MASTER SPEC §63-64, §92).

Runs after each session.  Grades P&L, opportunity/entry/exit/stop quality,
risk compliance, execution, data, AI and system health, and answers
WHY PROFIT / WHY LOSS / WHY NO TRADE / WHAT WAS MISSED in plain language.

The 100× pace is *reference display only* (§63): being behind pace never
raises risk — there is no code path from pace to risk parameters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from services.pipeline import PipelineResult


@dataclass
class DailyReview:
    day: date
    pnl: float
    equity: float
    start_equity: float
    target_multiple: float = 100.0
    horizon_days: int = 250
    day_index: int = 1
    result: PipelineResult | None = None
    risk_violations: int = 0
    data_issues: int = 0
    grades: dict[str, str] = field(default_factory=dict)

    @property
    def theoretical_pace_equity(self) -> float:
        """理論進捗の参考表示 (§63) — informational only, never feeds risk."""
        daily_growth = self.target_multiple ** (1 / self.horizon_days)
        return self.start_equity * daily_growth ** self.day_index

    def compute_grades(self) -> dict[str, str]:
        g: dict[str, str] = {}
        g["risk_compliance"] = "A" if self.risk_violations == 0 else "F"
        g["data_quality"] = "A" if self.data_issues == 0 else ("C" if self.data_issues < 3 else "F")
        if self.result:
            r = self.result
            g["opportunity_quality"] = ("A" if r.risk_passed > 0
                                        else "B" if r.decision_candidates > 0 else "C")
            g["execution_quality"] = ("A" if r.orders_filled == r.risk_passed
                                      else "B" if r.orders_filled > 0 else "C")
        g["pnl"] = "A" if self.pnl > 0 else ("B" if self.pnl == 0 else "C")
        # overall = worst individual grade; a risk violation dominates everything
        order = "FDCBA"
        g["overall"] = min(g.values(), key=order.index)
        self.grades = g
        return g

    def narrative(self) -> list[str]:
        """§64: WHY PROFIT / WHY LOSS / WHY NO TRADE / WHAT WAS MISSED."""
        lines: list[str] = []
        r = self.result
        if self.pnl > 0:
            lines.append(f"WHY PROFIT: {self.pnl:+.2f} — "
                         f"{r.orders_filled if r else 0} fills; mark-to-market gains held.")
        elif self.pnl < 0:
            lines.append(f"WHY LOSS: {self.pnl:+.2f} — positions marked down; "
                         "check stop quality and entry timing below.")
        if r and not r.traded:
            lines.append("WHY NO TRADE: " + (r.no_trade_reasons[0] if r.no_trade_reasons
                                             else "funnel produced no risk-passing candidate."))
        if r and r.skeptic_vetoes:
            lines.append(f"WHAT WAS MISSED: {r.skeptic_vetoes} candidates vetoed by skeptic — "
                         "review vetoes against outcomes (§53 counterfactual).")
        pace = self.theoretical_pace_equity
        lines.append(f"REFERENCE: theoretical 100x pace equity {pace:,.2f} vs actual "
                     f"{self.equity:,.2f} (informational only — never raises risk §63)")
        return lines
