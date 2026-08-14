"""Master Risk Controller (MASTER SPEC §42-43) — deterministic, no AI.

Every order intent passes ALL checks or is rejected with reasons.  Approvals
are signed with an HMAC key private to this controller; the Execution Engine
verifies the signature, so no other component (human or AI) can mint an
executable order (§7, INV-6, INV-8).

MASTER STOP semantics (§43): halting new entries never blocks protective
stops, risk-reducing sells or emergency liquidation.
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union

from packages.common.calendar import SessionStatus, TradingCalendar
from packages.common.clock import Clock
from packages.common.risk_config import RiskConfig
from packages.schemas.core import (
    Action,
    OrderIntent,
    RiskApproval,
    RiskCheck,
    RiskRejection,
)
from services.data_validation.integrity import DataHealth


class RiskState(str, enum.Enum):
    NORMAL = "NORMAL"
    HALT_NEW_ENTRIES = "HALT_NEW_ENTRIES"
    SAFE_EXIT = "SAFE_EXIT"
    FULL_BROKER_DISCONNECT = "FULL_BROKER_DISCONNECT"


class ThrottleLevel(str, enum.Enum):
    NORMAL = "NORMAL"
    REDUCED_SIZE = "REDUCED_SIZE"
    NO_NEW_ENTRY = "NO_NEW_ENTRY"
    REVIEW_SAFE_MODE = "REVIEW_SAFE_MODE"


def throttle_level(drawdown: float, cfg: RiskConfig) -> ThrottleLevel:
    """Drawdown throttling ladder (§39). Thresholds come from RiskConfig,
    which no AI can mutate at runtime (INV-13)."""
    if drawdown >= cfg.dd_safe_mode_at:
        return ThrottleLevel.REVIEW_SAFE_MODE
    if drawdown >= cfg.dd_no_new_entry_at:
        return ThrottleLevel.NO_NEW_ENTRY
    if drawdown >= cfg.dd_reduced_size_at:
        return ThrottleLevel.REDUCED_SIZE
    return ThrottleLevel.NORMAL


def throttle_factor(level: ThrottleLevel, cfg: RiskConfig) -> float:
    return cfg.reduced_size_factor if level is ThrottleLevel.REDUCED_SIZE else 1.0


@dataclass
class PortfolioRiskView:
    """Deterministic snapshot the controller judges against."""

    equity: float
    settled_cash: float
    total_exposure_notional: float
    position_notional: dict[str, float]
    position_qty: dict[str, float]
    theme_exposure: dict[str, float]
    symbol_themes: dict[str, list[str]]
    drawdown: float
    stop_plan_exists: bool
    gap_risk_score: float
    adv_shares: float
    correlation_to_book: float
    reconciliation_ok: bool
    data_health: DataHealth
    broker_connected: bool


@dataclass
class MasterRiskController:
    config: RiskConfig
    calendar: TradingCalendar
    clock: Clock
    state: RiskState = RiskState.NORMAL
    _signing_key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)
    decisions_log: list[Union[RiskApproval, RiskRejection]] = field(default_factory=list)

    # -- state management ---------------------------------------------------
    def set_state(self, state: RiskState, reason: str = "") -> None:
        self.state = state

    def _entry_allowed_by_state(self, throttle: ThrottleLevel) -> tuple[bool, str]:
        if self.state is RiskState.FULL_BROKER_DISCONNECT:
            return False, "broker disconnected"
        if self.state in (RiskState.HALT_NEW_ENTRIES, RiskState.SAFE_EXIT):
            return False, f"MASTER STOP active: {self.state.value}"
        if throttle in (ThrottleLevel.NO_NEW_ENTRY, ThrottleLevel.REVIEW_SAFE_MODE):
            return False, f"drawdown throttle: {throttle.value} (§39)"
        return True, ""

    def _exit_allowed_by_state(self) -> tuple[bool, str]:
        # §43: protective stop / risk-reducing SELL stay allowed under MASTER STOP
        if self.state is RiskState.FULL_BROKER_DISCONNECT:
            return False, "broker disconnected — nothing can be sent"
        return True, ""

    # -- signature ----------------------------------------------------------
    def _sign(self, intent: OrderIntent, approval_id: str) -> str:
        msg = f"{approval_id}|{intent.client_order_id}|{intent.symbol}|{intent.side.value}|{intent.qty}"
        return hmac.new(self._signing_key, msg.encode(), hashlib.sha256).hexdigest()

    def verify_signature(self, approval: RiskApproval) -> bool:
        msg = (f"{approval.approval_id}|{approval.client_order_id}|{approval.symbol}|"
               f"{approval.side.value}|{approval.qty}")
        expected = hmac.new(self._signing_key, msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, approval.signature)

    # -- the gate (§42) ------------------------------------------------------
    def review(self, intent: OrderIntent, view: PortfolioRiskView,
               entry_price: Optional[float] = None) -> Union[RiskApproval, RiskRejection]:
        cfg = self.config
        checks: list[RiskCheck] = []
        now = self.clock.now()
        is_exit = intent.side is Action.SELL and intent.is_protective_exit

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append(RiskCheck(name=name, passed=passed, detail=detail))

        px = entry_price or intent.limit_price or intent.stop_price or 0.0
        order_value = intent.qty * px

        throttle = throttle_level(view.drawdown, cfg)

        # 12. risk state / MASTER STOP / drawdown throttle
        if is_exit:
            ok, why = self._exit_allowed_by_state()
            check("risk_state", ok, why or f"exit allowed under {self.state.value} (§43)")
        else:
            ok, why = self._entry_allowed_by_state(throttle)
            check("risk_state", ok, why)

        # 1. leverage (INV-1): a cash purchase can never lever, but assert anyway
        if intent.side is Action.BUY:
            new_exposure = view.total_exposure_notional + order_value
            leverage = new_exposure / view.equity if view.equity > 0 else float("inf")
            check("leverage", leverage <= cfg.max_leverage + 1e-9,
                  f"would-be leverage {leverage:.3f}")
        else:
            check("leverage", True, "sell reduces exposure")

        # 2-3. cash & settlement (§14): only settled cash spendable
        if intent.side is Action.BUY:
            check("cash_settled", order_value <= view.settled_cash + 1e-9,
                  f"order {order_value:.2f} vs settled {view.settled_cash:.2f}")
        else:
            qty_held = view.position_qty.get(intent.symbol, 0.0)
            check("no_short", intent.qty <= qty_held + 1e-9,
                  f"sell {intent.qty} vs held {qty_held} (INV-3)")

        # 4. exposure cap (§36, INV-11) incl. capital ramp stage (§104)
        if intent.side is Action.BUY:
            max_total = view.equity * cfg.max_total_exposure_pct * cfg.exposure_cap_stage
            check("total_exposure",
                  view.total_exposure_notional + order_value <= max_total + 1e-9,
                  f"{view.total_exposure_notional + order_value:.2f} vs cap {max_total:.2f}")
        else:
            check("total_exposure", True)

        # 5. position size / liquidity (§41)
        if intent.side is Action.BUY:
            check("position_size", order_value <= view.equity * cfg.max_position_pct + 1e-9)
            liq_cap = view.adv_shares * cfg.max_adv_participation
            check("liquidity", intent.qty <= liq_cap + 1e-9,
                  f"qty {intent.qty} vs ADV cap {liq_cap:.0f}")
        else:
            check("position_size", True)
            check("liquidity", True)

        # 6. stop existence (INV-4)
        check("stop_exists", is_exit or view.stop_plan_exists,
              "every entry requires a stop plan (§33)")

        # 7. correlation / theme concentration (§38)
        if intent.side is Action.BUY:
            theme_ok = True
            detail = ""
            for theme in view.symbol_themes.get(intent.symbol, []):
                t_after = view.theme_exposure.get(theme, 0.0) + order_value
                if t_after > view.equity * cfg.max_theme_exposure_pct + 1e-9:
                    theme_ok, detail = False, f"theme {theme} would reach {t_after:.2f}"
            check("theme_concentration", theme_ok, detail)
        else:
            check("theme_concentration", True)

        # 8. gap risk (§40)
        check("gap_risk", is_exit or view.gap_risk_score <= cfg.max_gap_risk_score,
              f"score {view.gap_risk_score:.2f}")

        # 9. broker state (§48)
        check("broker_connected", view.broker_connected)
        check("reconciliation", is_exit or view.reconciliation_ok,
              "ledger/broker mismatch halts entries (§48)")

        # 10. market state (§13)
        status = self.calendar.status(now)
        market_ok = status is SessionStatus.OPEN
        check("market_open", market_ok or is_exit and status is not SessionStatus.HALTED,
              f"session status {status.value}")

        # 11. data health (§11)
        check("data_health", is_exit or view.data_health is not DataHealth.HALT_ENTRIES,
              f"data health {view.data_health.value}")

        failed = [c for c in checks if not c.passed]
        if failed:
            rejection = RiskRejection(client_order_id=intent.client_order_id,
                                      checks=tuple(checks),
                                      reasons=tuple(f"{c.name}: {c.detail}" for c in failed),
                                      rejected_at=now)
            self.decisions_log.append(rejection)
            return rejection

        approval_id = str(uuid.uuid4())
        approval = RiskApproval(approval_id=approval_id,
                                client_order_id=intent.client_order_id,
                                symbol=intent.symbol, side=intent.side, qty=intent.qty,
                                checks=tuple(checks), risk_state=self.state.value,
                                approved_at=now,
                                signature=self._sign(intent, approval_id))
        self.decisions_log.append(approval)
        return approval
