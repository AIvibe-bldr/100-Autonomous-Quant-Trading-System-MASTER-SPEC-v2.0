"""Quarterly Trend Detector (research-instruction §5).

"1四半期だけの改善で構造的改善と断定しない" — a run of `min_periods`
consecutive same-direction quarters is required before a metric counts as
IMPROVING/DETERIORATING; anything shorter, or with an undefined value in
the window, reads as INSUFFICIENT_DATA/FLAT rather than a guess.
"""
from __future__ import annotations

from typing import Optional, Sequence

from packages.schemas.fundamentals import FinancialMetrics, InflectionDirection, MetricTrend

# Tracked per the instruction's §4 list — the metrics whose trend actually
# matters for "is this company's growth/profitability structurally
# improving," not every field FinancialMetrics happens to carry.
TRACKED_METRICS = (
    "gross_margin", "operating_margin", "revenue_growth_yoy",
    "free_cash_flow", "roic",
)


def detect_trend(metric_name: str, values: Sequence[Optional[float]],
                 min_periods: int = 3) -> MetricTrend:
    """`values` ordered oldest -> newest (one entry per quarter, `None`
    where that quarter's value was undefined). Direction is decided by the
    trailing `min_periods` values only — an old streak that broke does not
    count as "still improving" just because it once held."""
    clean = [v for v in values if v is not None]
    if len(clean) < min_periods:
        return MetricTrend(metric_name=metric_name,
                           direction=InflectionDirection.INSUFFICIENT_DATA,
                           consecutive_periods=0,
                           latest_value=clean[-1] if clean else None,
                           values=tuple(clean))

    window = clean[-min_periods:]
    strictly_increasing = all(b > a for a, b in zip(window, window[1:]))
    strictly_decreasing = all(b < a for a, b in zip(window, window[1:]))
    if strictly_increasing:
        direction = InflectionDirection.IMPROVING
    elif strictly_decreasing:
        direction = InflectionDirection.DETERIORATING
    else:
        direction = InflectionDirection.FLAT

    consecutive = 0
    if direction is not InflectionDirection.FLAT:
        consecutive = 1
        for i in range(len(clean) - 1, 0, -1):
            step_matches = ((direction is InflectionDirection.IMPROVING
                            and clean[i] > clean[i - 1])
                           or (direction is InflectionDirection.DETERIORATING
                               and clean[i] < clean[i - 1]))
            if not step_matches:
                break
            consecutive += 1

    return MetricTrend(metric_name=metric_name, direction=direction,
                       consecutive_periods=consecutive, latest_value=clean[-1],
                       values=tuple(clean))


def compute_trends(metrics_history: Sequence[FinancialMetrics],
                   min_periods: int = 3) -> tuple[MetricTrend, ...]:
    """`metrics_history` ordered oldest -> newest, one `FinancialMetrics`
    per quarter for a single symbol."""
    trends = []
    for name in TRACKED_METRICS:
        values = [getattr(m, name) for m in metrics_history]
        trends.append(detect_trend(name, values, min_periods=min_periods))
    return tuple(trends)
