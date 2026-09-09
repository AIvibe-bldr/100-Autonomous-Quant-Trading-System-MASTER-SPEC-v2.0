"""Shared estimation primitives tests (docs/capital_cell_architecture.md
§17-25 pattern, §41 priority 9-13 building block)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.capital_cells.estimation import (
    InsufficientDataError,
    PointEstimate,
    RangeEstimate,
    require_sample_size,
)

AT = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)


class TestPointEstimate:
    def test_valid_estimate_constructs(self):
        e = PointEstimate(value=0.62, confidence=0.8, sample_size=40,
                          method_version="v1", as_of=AT)
        assert e.value == 0.62

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            PointEstimate(value=1.0, confidence=1.5, sample_size=10,
                          method_version="v1", as_of=AT)
        with pytest.raises(ValueError):
            PointEstimate(value=1.0, confidence=-0.1, sample_size=10,
                          method_version="v1", as_of=AT)

    def test_negative_sample_size_rejected(self):
        with pytest.raises(ValueError):
            PointEstimate(value=1.0, confidence=0.5, sample_size=-1,
                          method_version="v1", as_of=AT)


class TestRangeEstimate:
    def test_valid_range_constructs(self):
        r = RangeEstimate(low=5_000_000, base=8_000_000, high=12_000_000,
                          confidence=0.6, sample_size=30, method_version="v1", as_of=AT)
        assert r.low <= r.base <= r.high

    def test_unordered_range_rejected(self):
        with pytest.raises(ValueError):
            RangeEstimate(low=10, base=5, high=20, confidence=0.5,
                          sample_size=10, method_version="v1", as_of=AT)
        with pytest.raises(ValueError):
            RangeEstimate(low=1, base=20, high=10, confidence=0.5,
                          sample_size=10, method_version="v1", as_of=AT)


class TestRequireSampleSize:
    def test_passes_when_sample_size_meets_minimum(self):
        require_sample_size(30, minimum=30, context="test")  # no raise

    def test_raises_insufficient_data_below_minimum(self):
        with pytest.raises(InsufficientDataError):
            require_sample_size(5, minimum=30, context="test")
