"""Data Integrity Engine (MASTER SPEC §11).

Checks: missing data, duplicates, extreme prices, bid > ask, timestamp
anomalies, stale feeds, symbol mismatch, impossible volume, secondary-source
disagreement.  Critical anomalies halt new entries; if positions are exposed
without data, the system escalates to SAFE EXIT MODE.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from packages.common.clock import ensure_utc
from packages.schemas.core import Bar, Quote


class Severity(str, enum.Enum):
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class DataHealth(str, enum.Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"        # warnings present
    HALT_ENTRIES = "HALT_ENTRIES"  # critical: no new entries (§11)


@dataclass(frozen=True)
class IntegrityIssue:
    check: str
    severity: Severity
    symbol: str
    detail: str


@dataclass
class DataIntegrityEngine:
    max_daily_move: float = 0.5          # ±50% in a day is "extreme price"
    stale_after: timedelta = timedelta(minutes=15)
    max_volume: int = 10_000_000_000
    issues: list[IntegrityIssue] = field(default_factory=list)

    def _add(self, check: str, severity: Severity, symbol: str, detail: str) -> None:
        self.issues.append(IntegrityIssue(check, severity, symbol, detail))

    def validate_bars(self, symbol: str, bars: list[Bar]) -> list[IntegrityIssue]:
        before = len(self.issues)
        if not bars:
            self._add("missing_data", Severity.CRITICAL, symbol, "no bars returned")
            return self.issues[before:]
        seen_ts = set()
        prev_close = None
        for b in bars:
            if b.symbol != symbol:
                self._add("symbol_mismatch", Severity.CRITICAL, symbol, f"bar for {b.symbol}")
            if b.ts in seen_ts:
                self._add("duplicate_data", Severity.WARNING, symbol, f"dup ts {b.ts}")
            seen_ts.add(b.ts)
            if prev_close is not None and prev_close > 0:
                move = abs(b.close - prev_close) / prev_close
                if move > self.max_daily_move:
                    self._add("extreme_price", Severity.CRITICAL, symbol,
                              f"{move:.0%} move at {b.ts}")
            if b.volume > self.max_volume:
                self._add("impossible_volume", Severity.CRITICAL, symbol, f"volume {b.volume}")
            prev_close = b.close
        ts_list = [b.ts for b in bars]
        if ts_list != sorted(ts_list):
            self._add("timestamp_anomaly", Severity.CRITICAL, symbol, "bars out of order")
        return self.issues[before:]

    def validate_quote(self, q: Quote, now: datetime) -> list[IntegrityIssue]:
        before = len(self.issues)
        if q.crossed:
            self._add("bid_gt_ask", Severity.CRITICAL, q.symbol,
                      f"bid {q.bid} > ask {q.ask}; quote quarantined")
        age = ensure_utc(now) - ensure_utc(q.ts)
        if age > self.stale_after:
            self._add("stale_feed", Severity.CRITICAL, q.symbol, f"quote age {age}")
        if age < timedelta(0):
            self._add("timestamp_anomaly", Severity.CRITICAL, q.symbol, "quote from the future")
        return self.issues[before:]

    def check_secondary_disagreement(self, symbol: str, primary_px: float,
                                     secondary_px: float, tolerance: float = 0.02) -> None:
        if primary_px <= 0 or secondary_px <= 0:
            return
        diff = abs(primary_px - secondary_px) / primary_px
        if diff > tolerance:
            self._add("secondary_source_disagreement", Severity.CRITICAL, symbol,
                      f"primary {primary_px} vs secondary {secondary_px} ({diff:.1%})")

    @property
    def health(self) -> DataHealth:
        if any(i.severity is Severity.CRITICAL for i in self.issues):
            return DataHealth.HALT_ENTRIES
        if self.issues:
            return DataHealth.DEGRADED
        return DataHealth.OK

    def clear_resolved(self) -> None:
        self.issues.clear()
