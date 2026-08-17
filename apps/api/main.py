"""Status API (MASTER SPEC §86-97) — read-only backend for the UI.

Serves Simple Mode numbers (§86), the asset chart series (§87), holdings
(§88), theme exposure (§90), NO TRADE reasons (§93), feature center (§94)
and system health incl. the environment badge (§73) and MASTER STOP state
(§97).

Strictly read-only: no endpoint mutates risk settings or places orders —
the UI can never become a path around the risk pipeline (§2, §78).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DASHBOARD_HTML = Path(__file__).resolve().parents[1] / "web" / "dashboard.html"

from packages.common.risk_config import RiskConfig
from services.cost_manager.engine import OperatingCostEngine
from services.decision.monitor import (
    MockMonitorModel,
    MonitorContext,
    MonitorSupervisor,
    anomaly_present,
)
from services.feature_manager.store import FeatureStatus, FeatureStore
from services.pipeline import PipelineResult, TradingPipeline
from services.supervisor.heartbeat import HeartbeatRegistry


def create_app(pipeline: TradingPipeline,
               cost_engine: Optional[OperatingCostEngine] = None,
               feature_store: Optional[FeatureStore] = None,
               monitor: Optional[MonitorSupervisor] = None,
               heartbeats: Optional[HeartbeatRegistry] = None) -> FastAPI:
    app = FastAPI(title="Quant Trading Platform — Status API", version="0.1.0")
    cost_engine = cost_engine or OperatingCostEngine()
    feature_store = feature_store or FeatureStore()
    monitor = monitor or MonitorSupervisor(model=MockMonitorModel())
    last_result: dict[str, Any] = {"result": None}
    equity_series: list[dict[str, Any]] = []

    def _prices() -> dict[str, float]:
        now = pipeline.clock.now()
        return {s: pipeline.market_data.quote(s, now).mid for s in pipeline.ledger.positions}

    def record_session(result: PipelineResult) -> None:
        """Called by the session runner after each run_session."""
        last_result["result"] = result
        snap = pipeline.ledger.snapshot(_prices())
        equity_series.append({"at": pipeline.clock.now().isoformat(),
                              "equity": snap.equity})

    app.state.record_session = record_session

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        """Self-contained dashboard UI (§86-97). Read-only like every route."""
        return DASHBOARD_HTML.read_text()

    @app.get("/health")
    def health() -> dict[str, Any]:
        """§97 + §73: environment badge and MASTER STOP state are always visible."""
        from services.risk.master_controller import ThrottleLevel, throttle_level

        snap = pipeline.ledger.snapshot(_prices())
        throttle = throttle_level(snap.drawdown, pipeline.risk_controller.config)
        # The drawdown throttle is derived from drawdown, NOT from risk_state,
        # so a system locked out at NO_NEW_ENTRY previously reported
        # risk_state=NORMAL and was indistinguishable from a quiet day. Surface
        # it, and say plainly when only a human can clear it (§39, §69).
        entries_blocked = throttle in (ThrottleLevel.NO_NEW_ENTRY,
                                       ThrottleLevel.REVIEW_SAFE_MODE)
        return {
            "environment": pipeline.environment.value,   # PAPER / LIVE badge (§73)
            "risk_state": pipeline.risk_controller.state.value,
            "data_health": pipeline.integrity.health.value,
            "master_stop_available": True,               # §97: MASTER STOP常設
            "drawdown": snap.drawdown,
            "throttle_level": throttle.value,
            "new_entries_blocked": entries_blocked,
            "requires_human_to_resume": entries_blocked and not pipeline.ledger.positions,
        }

    @app.get("/portfolio")
    def portfolio() -> dict[str, Any]:
        """Simple Mode top block (§86)."""
        snap = pipeline.ledger.snapshot(_prices())
        start = pipeline.ledger.initial_cash
        trading_pnl = snap.equity - start
        two = cost_engine.two_pnl(trading_pnl)
        return {
            "environment": pipeline.environment.value,
            "current_equity": snap.equity,
            "start_equity": start,
            "cumulative_pnl": trading_pnl,
            "cumulative_pnl_pct": trading_pnl / start,
            "high_water_mark": snap.high_water_mark,
            "drawdown": snap.drawdown,
            "target_equity_100x": start * 100,
            "progress_to_100x": snap.equity / (start * 100),
            "trading_pnl": two.trading_pnl,               # §81: two P&L separated
            "operating_costs": two.operating_costs,
            "project_net_pnl": two.project_net_pnl,
        }

    @app.get("/chart")
    def chart() -> list[dict[str, Any]]:
        """Asset chart series (§87)."""
        return equity_series

    @app.get("/holdings")
    def holdings() -> list[dict[str, Any]]:
        """Holdings UI (§88) with theme tags (§89)."""
        prices = _prices()
        out = []
        for sym, lot in pipeline.ledger.positions.items():
            cur = prices[sym]
            out.append({
                "ticker": sym,
                "themes": pipeline.symbol_themes.get(sym, []),
                "entry": lot.avg_cost,
                "current": cur,
                "qty": lot.qty,
                "pnl": (cur - lot.avg_cost) * lot.qty,
                "pnl_pct": cur / lot.avg_cost - 1 if lot.avg_cost else 0.0,
                "notional": lot.qty * cur,
            })
        return out

    @app.get("/themes")
    def themes() -> dict[str, float]:
        """Theme exposure incl. cash (§90)."""
        prices = _prices()
        snap = pipeline.ledger.snapshot(prices)
        exposure: dict[str, float] = {}
        for sym, lot in pipeline.ledger.positions.items():
            notional = lot.qty * prices[sym]
            for theme in pipeline.symbol_themes.get(sym, ["Other"]):
                exposure[theme] = exposure.get(theme, 0.0) + notional
        pct = {t: v / snap.equity for t, v in exposure.items()} if snap.equity else {}
        pct["Cash"] = snap.cash / snap.equity if snap.equity else 0.0
        return pct

    @app.get("/session")
    def session() -> dict[str, Any]:
        """Last session funnel + NO TRADE reasons (§93: 故障ではなく理由表示)."""
        r: Optional[PipelineResult] = last_result["result"]
        if r is None:
            return {"status": "no session run yet"}
        return {
            "scanned": r.scanned, "candidates": r.candidates,
            "decision_candidates": r.decision_candidates,
            "risk_passed": r.risk_passed, "orders_filled": r.orders_filled,
            "traded": r.traded, "no_trade_reasons": r.no_trade_reasons,
        }

    @app.get("/features")
    def features() -> dict[str, list[str]]:
        """Feature Center (§94)."""
        return {status.value: [m.name for m in feature_store.by_status(status)]
                for status in FeatureStatus}

    @app.get("/decision-quality")
    def decision_quality() -> dict[str, Any]:
        """A7: monthly decision quality panel (ADDENDUM A2)."""
        from services.pdca.decision_quality import DecisionQualityReporter

        now = pipeline.clock.now()
        reporter = DecisionQualityReporter(pipeline.decision_quality)
        r = reporter.monthly(now.year, now.month)
        months = []
        y, m = now.year, now.month
        for _ in range(4):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        return {
            "month": r.month, "total_decisions": r.total,
            "good": r.counts.get("GOOD", 0), "good_pct": r.pct("GOOD"),
            "mixed": r.counts.get("MIXED", 0), "mixed_pct": r.pct("MIXED"),
            "bad": r.counts.get("BAD", 0), "bad_pct": r.pct("BAD"),
            "pending": r.counts.get("PENDING", 0), "pending_pct": r.pct("PENDING"),
            "average_decision_score": r.average_score,
            "by_kind": r.by_kind,                       # BUY/SELL/WAIT/NO_TRADE精度 (A2-1)
            "expected_value_per_decision": r.expected_value_per_decision,
            "avg_good_return": r.avg_good_return,       # A2-2: 勝率だけをKPIにしない
            "avg_bad_loss": r.avg_bad_loss,
            "avg_mae": r.avg_mae, "avg_mfe": r.avg_mfe,
            "confidence_calibration": r.confidence_buckets,   # A2-3
            "by_regime": r.by_regime,                          # A2-4
            "trend": reporter.trend(list(reversed(months))),   # A2-5
        }

    @app.get("/order-safety")
    def order_safety() -> dict[str, Any]:
        """A7: 発注安全性 panel — audits, prevented errors, reconciliation."""
        now = pipeline.clock.now()
        summary = pipeline.audit_log.monthly_safety_summary(now.year, now.month)
        summary["broker_reconciliation"] = (
            "OK" if pipeline.risk_controller.state.value == "NORMAL" else
            pipeline.risk_controller.state.value)
        return summary

    @app.get("/final-trade-theses")
    def final_trade_theses() -> list[dict[str, Any]]:
        """Final Trade Thesis panel (§27): each surviving BUY candidate this
        session, showing which Skeptic agent reviewed it and how much the
        Decision AI and Skeptic AI disagreed (`disagreement_score`) even
        though the Skeptic did not veto."""
        out = []
        for thesis in pipeline.final_theses().values():
            decision = thesis.proposal.decision
            skeptic = thesis.proposal.skeptic
            out.append({
                "decision_id": thesis.decision_id,
                "skeptic_id": thesis.skeptic_id,
                "symbol": thesis.proposal.symbol,
                "action": decision.action.value,
                "confidence": decision.confidence,
                "disagreement_score": thesis.disagreement_score,
                "skeptic_severity": skeptic.severity if skeptic else None,
                "skeptic_objections": list(skeptic.objections) if skeptic else [],
                "created_at": thesis.created_at.isoformat(),
            })
        out.sort(key=lambda t: t["created_at"], reverse=True)
        return out

    @app.get("/monitor")
    def monitor_status() -> dict[str, Any]:
        """Monitor AI panel (§66). Consulted only on anomaly — an unhealthy
        heartbeat, elevated drawdown, non-NORMAL risk state, or a recent
        near-miss — never on every refresh, so a quiet system costs no model
        call here either (`anomaly_present`)."""
        now = pipeline.clock.now()
        snap = pipeline.ledger.snapshot(_prices())
        recent_misses = tuple(
            f"{m.kind.value}: {m.detail}" if m.detail else m.kind.value
            for m in pipeline.audit_log.near_misses[-5:])
        ctx = MonitorContext(
            now=now,
            heartbeats=heartbeats.snapshot() if heartbeats else {},
            risk_state=pipeline.risk_controller.state.value,
            drawdown=snap.drawdown,
            open_positions={s: lot.qty for s, lot in pipeline.ledger.positions.items()},
            recent_near_misses=recent_misses)
        if not anomaly_present(ctx):
            # §66: nothing to report, and no model call spent reporting it
            return {"consulted": False, "recommendation": "CONTINUE_MONITORING",
                    "severity": 0.0,
                    "findings": ["no anomaly detected — Monitor AI not consulted (§66)"],
                    "risk_state": ctx.risk_state, "drawdown": ctx.drawdown}
        out = monitor.review(ctx)
        result = out.model_dump(mode="json")
        result["consulted"] = True
        return result

    @app.get("/risk-config")
    def risk_config() -> dict[str, Any]:
        """Read-only view of risk limits — no write endpoint exists (§39)."""
        cfg: RiskConfig = pipeline.risk_controller.config
        return {"version": cfg.version,
                "per_trade_risk_budget_pct": cfg.per_trade_risk_budget_pct,
                "max_position_pct": cfg.max_position_pct,
                "max_total_exposure_pct": cfg.max_total_exposure_pct,
                "max_leverage": cfg.max_leverage,
                "exposure_cap_stage": cfg.exposure_cap_stage}

    return app
