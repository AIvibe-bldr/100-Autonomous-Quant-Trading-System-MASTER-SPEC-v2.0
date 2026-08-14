"""Gap / Overnight Risk (MASTER SPEC §40).

A stop order is not a price guarantee — price can gap through the stop.
Score in [0,1] from the historical overnight gap distribution plus scheduled
event risk (earnings etc., §16).
"""
from __future__ import annotations

from packages.schemas.core import Bar


def gap_risk_score(bars: list[Bar], has_event_before_horizon: bool = False) -> float:
    if len(bars) < 3:
        return 1.0  # unknown history → assume worst
    gaps = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        if prev.close > 0:
            gaps.append(abs(cur.open - prev.close) / prev.close)
    gaps.sort()
    # 95th percentile overnight gap
    p95 = gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))]
    score = min(1.0, p95 / 0.10)  # a 10%+ p95 gap saturates the score
    if has_event_before_horizon:
        score = min(1.0, score + 0.35)  # earnings/FOMC etc. add risk (§16)
    return round(score, 4)
