"""Run the independent Judge over candidate alphas and report its RECOMMENDATIONS (MASTER SPEC §23-24, §60).

The Judge needs a long history (many trades, walk-forward windows), so it is run offline over daily
bars — not inside the live session. Its strongest output is `PROMOTE_RECOMMENDED`, which is only a
recommendation to shadow-run and then ask a human; nothing here (or anywhere in services/alpha_factory)
can promote an alpha or touch risk settings."""
from __future__ import annotations

from typing import Optional, Sequence

from packages.schemas.core import Bar
from services.alpha_factory.factory import Alpha, AlphaJudge, MeanReversionAlpha, MomentumAlpha


def judge_alphas(bars_by_symbol: dict[str, list[Bar]], alphas: Optional[Sequence[Alpha]] = None,
                 judge: Optional[AlphaJudge] = None) -> list[dict]:
    judge = judge or AlphaJudge()
    rows = []
    for alpha in alphas or [MomentumAlpha(), MeanReversionAlpha()]:
        for symbol, bars in sorted(bars_by_symbol.items()):
            v = judge.judge(alpha, bars)
            rows.append({"alpha": alpha.name, "version": getattr(alpha, "version", ""),
                         "symbol": symbol, "bars": len(bars), "stage": v.stage.value,
                         "recommendation": v.recommendation.value,
                         "trades": v.backtest.trades if v.backtest else 0,
                         "total_return": v.backtest.total_return if v.backtest else None,
                         "oos_return": v.oos_return, "p_value": v.p_value, "reasons": list(v.reasons)})
    return rows
