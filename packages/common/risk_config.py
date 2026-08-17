"""Risk configuration (MASTER SPEC §35, §39).

Thresholds live in a frozen dataclass loaded from a config file at startup.
No runtime API mutates them — changing risk limits requires editing the file,
which the spec gates behind Human Approval + Versioning + Test (§39).
AI agents have no code path that can construct or replace a RiskConfig.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class RiskConfig:
    version: str = "1.0.0"

    # Position sizing (§35)
    per_trade_risk_budget_pct: float = 0.01   # 1% of equity risked per trade
    max_position_pct: float = 0.25            # single position <= 25% of equity
    max_theme_exposure_pct: float = 0.40      # theme concentration cap (§38, §90)
    max_adv_participation: float = 0.01       # <= 1% of daily volume (§41)
    correlation_penalty_threshold: float = 0.7
    correlation_penalty_factor: float = 0.5   # halve size when highly correlated (§38)

    # Exposure (§36)
    max_total_exposure_pct: float = 1.00      # Total Exposure <= 100%, absolute (§36)

    # Leverage (§2)
    max_leverage: float = 1.0                 # immutable ceiling; also enforced by INV-1

    # Drawdown throttling (§39): NORMAL → REDUCED_SIZE → NO_NEW_ENTRY → REVIEW_SAFE_MODE
    dd_reduced_size_at: float = 0.10
    dd_no_new_entry_at: float = 0.20
    dd_safe_mode_at: float = 0.30
    reduced_size_factor: float = 0.5

    # Gap / overnight risk (§40)
    max_gap_risk_score: float = 0.75

    # Spread cap (§10) — a spread this wide eats the expected edge
    max_spread_pct: float = 0.01

    # Stale order control (§47)
    stale_order_after_sec: float = 300.0

    # Live capital ramp (§104) — fraction of account allowed as exposure
    exposure_cap_stage: float = 1.0

    def __post_init__(self) -> None:
        if self.max_leverage > 1.0:
            raise ValueError("max_leverage may never exceed 1.0 (IMMUTABLE SAFETY RULE §2)")
        if self.max_total_exposure_pct > 1.0:
            raise ValueError("total exposure cap may never exceed 100% (§36)")
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float) and v < 0:
                raise ValueError(f"negative risk parameter {f.name}={v}")

    @classmethod
    def load(cls, path: Path) -> "RiskConfig":
        data = json.loads(path.read_text())
        return cls(**data)


DEFAULT_RISK_CONFIG = RiskConfig()
