"""SAFETY_AUDIT F12: RiskConfig must reject NaN/Infinity in its own fields
(NaN passes every `<`/`>` comparison, so the range checks alone miss it)."""
import json

import pytest

from packages.common.risk_config import RiskConfig


@pytest.mark.parametrize("field", ["per_trade_risk_budget_pct", "max_position_pct",
                                   "max_theme_exposure_pct", "max_spread_pct",
                                   "dd_safe_mode_at", "max_gap_risk_score",
                                   "stale_order_after_sec", "max_total_exposure_pct"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_risk_parameter_is_rejected(field, bad):
    with pytest.raises(ValueError):
        RiskConfig(**{field: bad})


def test_non_finite_values_in_a_config_file_are_rejected(tmp_path):
    p = tmp_path / "risk.json"
    p.write_text('{"max_position_pct": NaN}')        # json.loads accepts NaN by default
    with pytest.raises(ValueError):
        RiskConfig.load(p)
