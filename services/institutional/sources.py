"""Where institutional-flow facts come from (MASTER SPEC §20) — only what can be had honestly.

Available without a paid feed:
- VOLUME_ANOMALY, computed from our own daily bars (direction = the day's price move);
- SEC Schedule 13D/13G filings (that an ownership report was filed; the index does not say
  whether the holder bought or sold, so these are stated as facts and carry no direction).
Not available (stated as such to the model, never invented): block trades, order-flow
imbalance, options flow, short interest, ETF flows. §20: no single feature is ever a reason
to trade — `InstitutionalSignal.actionable` needs two independent agreeing features."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional, Protocol, Sequence

from packages.schemas.core import Bar
from services.institutional.engine import FlowFeature, FlowObservation
from services.market_data.edgar_client import EdgarClient, EdgarUnavailable

VOLUME_WINDOW = 20
MIN_VOLUME_RATIO = 1.5
FILINGS_LOOKBACK_DAYS = 45
_OWNERSHIP_FORM_PREFIXES = ("SCHEDULE 13D", "SCHEDULE 13G", "SC 13D", "SC 13G")


class FlowUnavailable(RuntimeError):
    """Ownership filings could not be obtained (never read as 'none')."""


class OwnershipFilingsSource(Protocol):
    def fetch(self, symbols: list[str], today: date) -> dict[str, list[str]]:
        """symbol -> one factual line per recent ownership filing."""
        ...


def volume_anomaly(symbol: str, bars: Sequence[Bar]) -> Optional[tuple[FlowObservation, float]]:
    """(observation, volume ratio) when the latest bar's volume is at least MIN_VOLUME_RATIO times
    the mean of the previous VOLUME_WINDOW bars; value = sign of the day's move * min(1, (ratio-1)/2).
    None when there is no anomaly or too little history."""
    if len(bars) < VOLUME_WINDOW + 1:
        return None
    last, prev = bars[-1], bars[-VOLUME_WINDOW - 1:-1]
    mean = sum(b.volume for b in prev) / VOLUME_WINDOW
    if mean <= 0:
        return None
    ratio = last.volume / mean
    if ratio < MIN_VOLUME_RATIO:
        return None
    move = last.close - bars[-2].close
    sign = 1.0 if move > 0 else -1.0 if move < 0 else 0.0
    value = sign * min(1.0, (ratio - 1.0) / 2.0)
    return FlowObservation(symbol=symbol, feature=FlowFeature.VOLUME_ANOMALY, value=value,
                           observed_at=last.ts, source="own-bars"), ratio


def describe_flow(symbol: str, bars: Sequence[Bar],
                  filings: Optional[list[str]]) -> tuple[list[str], Optional[FlowObservation]]:
    """Fact lines for the prompt (`filings=None` = not fetched; the renderer says so) and the
    observation to feed the flow engine, if any."""
    lines: list[str] = []
    obs: Optional[FlowObservation] = None
    if len(bars) < VOLUME_WINDOW + 1:
        lines.append("volume anomaly: not enough history to judge")
    else:
        mean = sum(b.volume for b in bars[-VOLUME_WINDOW - 1:-1]) / VOLUME_WINDOW
        ratio = bars[-1].volume / mean if mean > 0 else 0.0
        found = volume_anomaly(symbol, bars)
        if found is None:
            lines.append(f"volume anomaly: none (latest volume {ratio:.1f}x its {VOLUME_WINDOW}-day average)")
        else:
            obs = found[0]
            move = bars[-1].close / bars[-2].close - 1.0
            lines.append(f"volume anomaly: latest volume {ratio:.1f}x its {VOLUME_WINDOW}-day average "
                         f"on a {move:+.1%} day (direction is taken from the price move)")
    if filings is not None:
        lines += [f"ownership filing: {x}" for x in filings] or [
            f"ownership filings (SEC 13D/13G): none in the last {FILINGS_LOOKBACK_DAYS} days"]
    return lines, obs


class EdgarOwnershipFilingsSource:
    def __init__(self, client: EdgarClient, lookback_days: int = FILINGS_LOOKBACK_DAYS) -> None:
        self._client = client
        self._lookback = timedelta(days=lookback_days)

    def fetch(self, symbols: list[str], today: date) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        try:
            for sym in symbols:
                lines = [
                    f"{f.filing_date.isoformat()} {f.form} filed (ownership report; the SEC index does "
                    f"not say whether the holder bought or sold)"
                    for f in self._client.recent_filings(sym)
                    if f.form.upper().startswith(_OWNERSHIP_FORM_PREFIXES)
                    and today - self._lookback <= f.filing_date <= today]
                out[sym] = lines[:5]
        except EdgarUnavailable as e:
            raise FlowUnavailable(str(e)) from None
        return out
