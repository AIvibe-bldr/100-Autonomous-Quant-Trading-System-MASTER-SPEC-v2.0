"""Uncertainty-carrying estimate primitives (docs/capital_cell_architecture.md
§17-25, shared building block for §41 priority 9-13).

The same shape repeats across the doc: a number that feeds Capital
Allocation is never ground truth. Every estimate here carries how it was
produced (a version-tagged method, so switching estimation approaches later
is a visible change, not a silent behavior shift — §18, §25) and how much to
trust it (`confidence`, `sample_size`). A caller with too little data gets
`InsufficientDataError` instead of a fabricated point estimate — the doc's
own `INSUFFICIENT_DATA` (§17, §24-25).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class InsufficientDataError(ValueError):
    """Not enough samples to support an estimate (§17, §24-25:
    INSUFFICIENT_DATA). Raised instead of returning a number nobody should
    trust — silently returning 0.0 or an unstated default would be read as
    a real estimate by anything downstream that doesn't check confidence."""


@dataclass(frozen=True)
class PointEstimate:
    """A single number this module is willing to stand behind, with the
    provenance to judge it by."""

    value: float
    confidence: float          # 0.0-1.0
    sample_size: int
    method_version: str
    as_of: datetime

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.sample_size < 0:
            raise ValueError("sample_size cannot be negative")


@dataclass(frozen=True)
class RangeEstimate:
    """low/base/high with confidence — for anything the doc explicitly
    refuses to collapse into one number: Capacity (§23), Marginal Alpha
    (§24), Scale Simulation (§25)."""

    low: float
    base: float
    high: float
    confidence: float
    sample_size: int
    method_version: str
    as_of: datetime

    def __post_init__(self) -> None:
        if not (self.low <= self.base <= self.high):
            raise ValueError(
                f"range must be ordered low<=base<=high, got "
                f"{self.low}, {self.base}, {self.high}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.sample_size < 0:
            raise ValueError("sample_size cannot be negative")


def require_sample_size(sample_size: int, minimum: int, context: str) -> None:
    """Shared gate every estimator in this package calls before computing
    anything, so "too little data" fails the same way everywhere rather
    than each module inventing its own threshold check (or forgetting
    one)."""
    if sample_size < minimum:
        raise InsufficientDataError(
            f"{context}: sample_size={sample_size} < minimum={minimum}")
