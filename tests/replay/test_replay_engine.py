"""Replay Engine tests (MASTER SPEC §62) + status API smoke test."""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.quant.replay import ReplayEngine
from tests.conftest import build_pipeline


def test_replay_runs_multiple_sessions(clock, universe):
    pipeline = build_pipeline(clock, universe)
    report = ReplayEngine(pipeline).run(start=date(2026, 8, 3), sessions=5)
    assert report.total_sessions == 5
    assert report.all_sessions_accounted  # §93: trade or explained
    assert report.final_equity > 0


def test_replay_is_reproducible(clock, universe):
    """§62: same start, same data → identical replay."""
    r1 = ReplayEngine(build_pipeline(clock, universe)).run(start=date(2026, 8, 3), sessions=3)
    r2 = ReplayEngine(build_pipeline(clock, universe)).run(start=date(2026, 8, 3), sessions=3)
    assert [d.equity for d in r1.days] == [d.equity for d in r2.days]


def test_replay_with_chaos_hook(clock, universe):
    """Fault injection per day (§62, §70): disconnect day 2, verify safe state."""
    from packages.broker_adapters.paper import Fault
    from services.risk.master_controller import RiskState

    pipeline = build_pipeline(clock, universe)
    days_seen: list[date] = []

    def chaos(d: date, p) -> None:
        days_seen.append(d)
        p.execution.broker.fault = Fault.DISCONNECT if len(days_seen) == 2 else Fault.NONE

    report = ReplayEngine(pipeline, on_day=chaos).run(start=date(2026, 8, 3), sessions=3)
    assert report.total_sessions == 3
    # after a disconnect the controller is in a safe non-normal state or recovered
    assert pipeline.risk_controller.state in (RiskState.FULL_BROKER_DISCONNECT,
                                              RiskState.HALT_NEW_ENTRIES,
                                              RiskState.NORMAL)


def test_status_api_smoke(clock, universe):
    pipeline = build_pipeline(clock, universe)
    app = create_app(pipeline)
    client = TestClient(app)

    result = pipeline.run_session(clock.now())
    app.state.record_session(result)

    health = client.get("/health").json()
    assert health["environment"] == "PAPER"  # §73: environment badge
    portfolio = client.get("/portfolio").json()
    assert {"trading_pnl", "operating_costs", "project_net_pnl"} <= set(portfolio)  # §81
    session = client.get("/session").json()
    assert "no_trade_reasons" in session  # §93
    assert client.get("/holdings").status_code == 200
    assert client.get("/themes").status_code == 200
    assert client.get("/risk-config").status_code == 200
    # strictly read-only: no mutating endpoints exist
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        assert not ({"POST", "PUT", "PATCH", "DELETE"} & methods), route.path
