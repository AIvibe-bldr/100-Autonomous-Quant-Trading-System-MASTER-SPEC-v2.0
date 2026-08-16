"""Decision Quality Engine (MASTER SPEC ADDENDUM A1) and the Monthly
Decision Quality Report (A2).

Extends the existing post-trade trackers (§50-53) upward: EVERY decision —
BUY / SELL / HOLD / WAIT / NO_TRADE / AVOID — is snapshotted immutably at
decision time (A1-1), tracked over fixed horizons (A1-2), and graded with
Outcome and Process SEPARATED (A1-5):

- Outcome Score: what the price actually did afterwards
- Process Score: was the decision sound given what was knowable then
- Overall: combined, with rule violations capping the result — a lucky
  rule-breaking win must never teach the AI that breaking rules works.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from packages.common.clock import ensure_utc

TRACK_HORIZONS: dict[str, timedelta] = {
    "1h": timedelta(hours=1), "1d": timedelta(days=1), "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
}
EXTENDED_HORIZONS: dict[str, timedelta] = {
    "1m": timedelta(days=30), "3m": timedelta(days=91), "6m": timedelta(days=182),
}
_HORIZON_ALIASES = {**TRACK_HORIZONS, **EXTENDED_HORIZONS}


class DecisionKind(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    AVOID = "AVOID"


class OutcomeClass(str, enum.Enum):
    GOOD = "GOOD"
    MIXED = "MIXED"
    BAD = "BAD"
    PENDING = "PENDING"


class AvoidanceLabel(str, enum.Enum):
    """Internal labels for NO_TRADE / WAIT / AVOID outcomes (A1-3)."""

    GOOD_AVOIDANCE = "GOOD_AVOIDANCE"
    MISSED_OPPORTUNITY = "MISSED_OPPORTUNITY"


class DecisionSnapshot(BaseModel):
    """A1-1: immutable record of what was decided and what was knowable.
    Frozen — any 'change' requires a NEW decision_id (INV-21)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    symbol: str
    ts: datetime
    reference_price: float = Field(gt=0)
    decision: DecisionKind
    confidence: float = Field(ge=0.0, le=1.0)
    expected_horizon: str
    expected_return_range: tuple[float, float]
    scenarios: dict[str, Any] = Field(default_factory=dict)   # bull/base/bear
    stop_plan: Optional[dict[str, Any]] = None
    thesis: str = ""
    invalidation_conditions: tuple[str, ...] = ()
    alpha_scores: dict[str, float] = Field(default_factory=dict)
    news_signals: tuple[str, ...] = ()
    institutional_signals: tuple[str, ...] = ()
    regime: str = "UNKNOWN"
    portfolio_context: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    model_version: str = ""
    prompt_version: str = ""
    risk_context: dict[str, Any] = Field(default_factory=dict)
    # process facts, recorded AT decision time (A1-5)
    rule_compliant: bool = True
    had_stop_plan: bool = False
    skeptic_consulted: bool = False
    data_health_ok: bool = True


@dataclass
class HorizonObservation:
    horizon: str
    price: float
    ret: float                       # signed return vs reference
    mae: float                       # max adverse excursion up to this horizon
    mfe: float                       # max favorable excursion up to this horizon
    observed_at: datetime


@dataclass
class DecisionEvaluation:
    decision_id: str
    outcome_class: OutcomeClass = OutcomeClass.PENDING
    avoidance_label: Optional[AvoidanceLabel] = None
    outcome_score: float = 50.0      # 0-100
    process_score: float = 50.0      # 0-100
    overall_score: float = 50.0      # 0-100
    observations: dict[str, HorizonObservation] = field(default_factory=dict)


class SnapshotTamperError(RuntimeError):
    """INV-21: an existing decision_id cannot be re-recorded with new content."""


def _direction(kind: DecisionKind) -> float:
    """+1 if the decision benefits from the price rising, -1 if from falling,
    0 for neutral stances."""
    if kind in (DecisionKind.BUY, DecisionKind.HOLD):
        return 1.0
    if kind is DecisionKind.SELL:
        return -1.0
    return 0.0  # WAIT / NO_TRADE / AVOID judged via avoidance labels


class DecisionQualityEngine:
    def __init__(self, good_threshold: float = 0.02, bad_threshold: float = 0.02) -> None:
        self._snapshots: dict[str, DecisionSnapshot] = {}
        self._evals: dict[str, DecisionEvaluation] = {}
        self.good_threshold = good_threshold
        self.bad_threshold = bad_threshold

    # -- A1-1: immutable snapshots ------------------------------------------
    def record(self, snap: DecisionSnapshot) -> None:
        existing = self._snapshots.get(snap.decision_id)
        if existing is not None:
            if existing != snap:
                raise SnapshotTamperError(
                    f"decision {snap.decision_id} already recorded — changes require "
                    "a NEW decision_id (INV-21)")
            return  # identical re-record is a no-op
        self._snapshots[snap.decision_id] = snap
        self._evals[snap.decision_id] = DecisionEvaluation(decision_id=snap.decision_id)

    def snapshot(self, decision_id: str) -> DecisionSnapshot:
        return self._snapshots[decision_id]

    def evaluation(self, decision_id: str) -> DecisionEvaluation:
        return self._evals[decision_id]

    # -- A1-2: tracking ------------------------------------------------------
    def track(self, now: datetime, price_fn: Callable[[str, datetime], float],
              include_extended: bool = False) -> int:
        """Observe matured horizons for every recorded decision."""
        now = ensure_utc(now)
        horizons = dict(TRACK_HORIZONS)
        if include_extended:
            horizons.update(EXTENDED_HORIZONS)
        observed = 0
        for did, snap in self._snapshots.items():
            ev = self._evals[did]
            targets = dict(horizons)
            expected = _HORIZON_ALIASES.get(snap.expected_horizon)
            if expected is not None:
                targets["expected"] = expected
            for label, delta in targets.items():
                due = ensure_utc(snap.ts) + delta
                if label in ev.observations or due > now:
                    continue
                px = price_fn(snap.symbol, due)
                ret = px / snap.reference_price - 1
                prior = [o for o in ev.observations.values()]
                mae = min([ret] + [o.mae for o in prior]) if prior else min(0.0, ret)
                mfe = max([ret] + [o.mfe for o in prior]) if prior else max(0.0, ret)
                ev.observations[label] = HorizonObservation(
                    horizon=label, price=px, ret=ret, mae=mae, mfe=mfe, observed_at=due)
                observed += 1
            self._evaluate(snap, ev)
        return observed

    # -- A1-3..A1-5: grading -------------------------------------------------
    def _evaluate(self, snap: DecisionSnapshot, ev: DecisionEvaluation) -> None:
        key = "expected" if "expected" in ev.observations else (
            "1w" if "1w" in ev.observations else None)
        if key is None:
            ev.outcome_class = OutcomeClass.PENDING
            return
        obs = ev.observations[key]
        d = _direction(snap.decision)

        # ---- outcome score (what happened) --------------------------------
        if d != 0.0:
            signed = obs.ret * d
            if signed >= self.good_threshold:
                ev.outcome_class = OutcomeClass.GOOD
            elif signed <= -self.bad_threshold:
                ev.outcome_class = OutcomeClass.BAD
            else:
                ev.outcome_class = OutcomeClass.MIXED
            lo, hi = snap.expected_return_range
            span = max(1e-9, hi - lo)
            ev.outcome_score = max(0.0, min(100.0, 50 + 50 * signed / span))
        else:
            # A1-4: WAIT / NO_TRADE / AVOID are graded too
            if obs.ret <= -self.bad_threshold:
                ev.avoidance_label = AvoidanceLabel.GOOD_AVOIDANCE
                ev.outcome_class = OutcomeClass.GOOD
                ev.outcome_score = min(100.0, 50 - 100 * obs.ret)
            elif obs.ret >= self.good_threshold:
                ev.avoidance_label = AvoidanceLabel.MISSED_OPPORTUNITY
                ev.outcome_class = OutcomeClass.MIXED   # a miss is not a BAD process
                ev.outcome_score = max(0.0, 50 - 100 * obs.ret)
            else:
                ev.outcome_class = OutcomeClass.MIXED
                ev.outcome_score = 50.0

        # ---- process score (was the decision sound at the time) -----------
        p = 100.0
        if not snap.rule_compliant:
            p -= 60.0            # rule violations dominate (A1-5)
        if snap.decision in (DecisionKind.BUY, DecisionKind.SELL) and not snap.had_stop_plan:
            p -= 30.0            # entering without an exit plan (§33)
        if not snap.skeptic_consulted:
            p -= 10.0
        if not snap.data_health_ok:
            p -= 20.0
        ev.process_score = max(0.0, p)

        # ---- overall (A1-5): lucky rule-breaking never scores well --------
        overall = 0.45 * ev.outcome_score + 0.55 * ev.process_score
        if not snap.rule_compliant:
            overall = min(overall, 40.0)
        ev.overall_score = round(overall, 2)


# ---------------------------------------------------------------------------
# A2: Monthly Decision Quality Report
# ---------------------------------------------------------------------------

@dataclass
class MonthlyReport:
    month: str                                   # "2026-08"
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)          # class -> n
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)
    average_score: float = 0.0
    avg_good_return: float = 0.0
    avg_bad_loss: float = 0.0
    expected_value_per_decision: float = 0.0
    avg_mae: float = 0.0
    avg_mfe: float = 0.0
    confidence_buckets: dict[str, dict[str, float]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)

    def pct(self, cls: str) -> float:
        return self.counts.get(cls, 0) / self.total if self.total else 0.0


def _bucket(confidence: float) -> str:
    if confidence >= 0.9:
        return ">=90%"
    if confidence >= 0.8:
        return "80-89%"
    if confidence >= 0.7:
        return "70-79%"
    return "<70%"


class DecisionQualityReporter:
    def __init__(self, engine: DecisionQualityEngine) -> None:
        self.engine = engine

    def monthly(self, year: int, month: int) -> MonthlyReport:
        key = f"{year:04d}-{month:02d}"
        report = MonthlyReport(month=key)
        scores: list[float] = []
        good_rets: list[float] = []
        bad_rets: list[float] = []
        all_rets: list[float] = []
        maes: list[float] = []
        mfes: list[float] = []
        kind_stats: dict[str, dict[str, int]] = {}
        conf_stats: dict[str, dict[str, int]] = {}
        regime_stats: dict[str, dict[str, int]] = {}

        for did, snap in self.engine._snapshots.items():
            ts = ensure_utc(snap.ts)
            if (ts.year, ts.month) != (year, month):
                continue
            ev = self.engine._evals[did]
            report.total += 1
            cls = ev.outcome_class.value
            report.counts[cls] = report.counts.get(cls, 0) + 1

            k = snap.decision.value
            kind_stats.setdefault(k, {"total": 0, "good": 0})
            kind_stats[k]["total"] += 1
            b = _bucket(snap.confidence)
            conf_stats.setdefault(b, {"total": 0, "good": 0})
            conf_stats[b]["total"] += 1
            r = snap.regime
            regime_stats.setdefault(r, {"total": 0, "good": 0})
            regime_stats[r]["total"] += 1

            if ev.outcome_class is OutcomeClass.PENDING:
                continue
            scores.append(ev.overall_score)
            if ev.outcome_class is OutcomeClass.GOOD:
                kind_stats[k]["good"] += 1
                conf_stats[b]["good"] += 1
                regime_stats[r]["good"] += 1

            key_obs = "expected" if "expected" in ev.observations else (
                "1w" if "1w" in ev.observations else None)
            if key_obs:
                obs = ev.observations[key_obs]
                d = _direction(snap.decision)
                signed = obs.ret * d if d != 0.0 else -obs.ret  # avoidance: down=win
                all_rets.append(signed)
                if ev.outcome_class is OutcomeClass.GOOD:
                    good_rets.append(signed)
                elif ev.outcome_class is OutcomeClass.BAD:
                    bad_rets.append(signed)
                maes.append(obs.mae)
                mfes.append(obs.mfe)

        report.average_score = round(sum(scores) / len(scores), 2) if scores else 0.0
        report.avg_good_return = (sum(good_rets) / len(good_rets)) if good_rets else 0.0
        report.avg_bad_loss = (sum(bad_rets) / len(bad_rets)) if bad_rets else 0.0
        # A2-2: EV per decision — win rate × avg win + loss rate × avg loss
        report.expected_value_per_decision = (sum(all_rets) / len(all_rets)) if all_rets else 0.0
        report.avg_mae = (sum(maes) / len(maes)) if maes else 0.0
        report.avg_mfe = (sum(mfes) / len(mfes)) if mfes else 0.0
        report.by_kind = {k: {"total": v["total"],
                              "good_pct": v["good"] / v["total"] if v["total"] else 0.0}
                          for k, v in kind_stats.items()}
        report.confidence_buckets = {b: {"total": v["total"],
                                         "good_pct": v["good"] / v["total"] if v["total"] else 0.0}
                                     for b, v in conf_stats.items()}
        report.by_regime = {r: {"total": v["total"],
                                "good_pct": v["good"] / v["total"] if v["total"] else 0.0}
                            for r, v in regime_stats.items()}
        return report

    def trend(self, months: list[tuple[int, int]]) -> list[dict[str, Any]]:
        """A2-5: monthly GOOD% trend. Interpretation caveat: a rising GOOD%
        is NOT automatically 'the AI improved' — regimes shift; the report
        carries by_regime so the caller can control for it.

        `good_pct` uses the same denominator as the monthly report (all
        decisions, matching the spec's headline example) so the trend and the
        panel never disagree; `resolved_good_pct` excludes PENDING for
        month-over-month comparison of matured decisions only."""
        out = []
        for y, m in months:
            r = self.monthly(y, m)
            resolved = r.total - r.counts.get("PENDING", 0)
            good = r.counts.get("GOOD", 0)
            out.append({"month": r.month, "total": r.total,
                        "good_pct": r.pct("GOOD"),
                        "resolved_good_pct": (good / resolved) if resolved else 0.0,
                        "average_score": r.average_score,
                        "by_regime": r.by_regime})
        return out
