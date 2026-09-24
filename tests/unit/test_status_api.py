"""Status API tests for the Final Trade Thesis and Monitor AI panels.

Read-only contract (§39, §78): neither endpoint accepts a body or mutates
anything — both just project existing pipeline/risk-controller state.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.common.clock import FrozenClock
from services.decision.monitor import MockMonitorModel, MonitorSupervisor
from services.risk.master_controller import RiskState
from services.supervisor.heartbeat import Heartbeat, HeartbeatRegistry, ServiceStatus
from tests.conftest import SYMBOLS, build_pipeline
from services.market_data.universe import UniverseManager, UniverseSymbol

SESSION_TIME = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


def _universe() -> UniverseManager:
    um = UniverseManager()
    for s in SYMBOLS:
        um.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    return um


@pytest.fixture
def api_pipeline():
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    pipeline.run_session(clock.now())
    return pipeline


# --- /final-trade-theses ------------------------------------------------------

def test_final_trade_theses_endpoint_reflects_pipeline_state(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/final-trade-theses")
    assert resp.status_code == 200
    body = resp.json()
    expected = api_pipeline.final_theses()
    assert len(body) == len(expected)
    if body:
        row = body[0]
        assert {"decision_id", "skeptic_id", "symbol", "action", "confidence",
                "disagreement_score", "skeptic_severity", "skeptic_objections",
                "created_at"} <= row.keys()
        thesis = expected[row["decision_id"]]
        assert row["skeptic_id"] == thesis.skeptic_id
        assert row["disagreement_score"] == thesis.disagreement_score
        assert 0.0 <= row["disagreement_score"] <= 1.0


def test_final_trade_theses_empty_when_no_session_ran():
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    client = TestClient(create_app(pipeline))
    resp = client.get("/final-trade-theses")
    assert resp.status_code == 200
    assert resp.json() == []


def test_final_trade_theses_endpoint_is_read_only(api_pipeline):
    """No POST/PUT/DELETE exists — the dashboard can only observe."""
    client = TestClient(create_app(api_pipeline))
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)("/final-trade-theses")
        assert resp.status_code == 405


# --- /monitor ------------------------------------------------------------------

def test_monitor_endpoint_not_consulted_when_healthy(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/monitor")
    assert resp.status_code == 200
    body = resp.json()
    assert body["consulted"] is False
    assert body["recommendation"] == "CONTINUE_MONITORING"
    assert body["severity"] == 0.0


def test_monitor_endpoint_consulted_on_risk_state_anomaly(api_pipeline):
    api_pipeline.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason="test")
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/monitor")
    body = resp.json()
    assert body["consulted"] is True
    assert body["recommendation"] in {"NOTIFY", "QUEUE_HUMAN_REVIEW", "ESCALATE_SAFE_EXIT"}
    assert any("HALT_NEW_ENTRIES" in f for f in body["findings"])


def test_monitor_endpoint_consulted_on_unhealthy_heartbeat(api_pipeline):
    clock = api_pipeline.clock
    hb = HeartbeatRegistry(clock=clock)
    hb.report(Heartbeat(service="scanner", at=clock.now(), status=ServiceStatus.ERROR,
                        last_success=clock.now()))
    client = TestClient(create_app(api_pipeline, heartbeats=hb))
    resp = client.get("/monitor")
    body = resp.json()
    assert body["consulted"] is True
    assert body["severity"] >= 0.9


def test_monitor_endpoint_uses_injected_supervisor(api_pipeline):
    api_pipeline.risk_controller.set_state(RiskState.HALT_NEW_ENTRIES, reason="test")
    calls: list[str] = []

    class _TrackingModel:
        name = "tracking-monitor"
        model_family = "tracking-family"

        def review(self, context):
            calls.append("called")
            return MockMonitorModel().review(context)

    client = TestClient(create_app(api_pipeline,
                                   monitor=MonitorSupervisor(model=_TrackingModel())))
    client.get("/monitor")
    assert calls == ["called"]


def test_monitor_endpoint_never_calls_model_when_no_anomaly(api_pipeline):
    """§66 cost contract: a quiet system must not spend a model call."""
    calls: list[str] = []

    class _ExplodingIfCalled:
        name = "should-not-be-called"
        model_family = "x"

        def review(self, context):
            calls.append("called")
            raise AssertionError("Monitor AI must not be consulted without an anomaly")

    client = TestClient(create_app(api_pipeline,
                                   monitor=MonitorSupervisor(model=_ExplodingIfCalled())))
    resp = client.get("/monitor")
    assert resp.status_code == 200
    assert calls == []


def test_monitor_endpoint_is_read_only(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)("/monitor")
        assert resp.status_code == 405


# --- /features -----------------------------------------------------------

def test_features_endpoint_reflects_the_pipelines_own_registrations(api_pipeline):
    """create_app() used to always construct a fresh, empty FeatureStore
    when none was passed explicitly — identical to the cost_engine bug
    this file's docstring precedent already fixed — so /features could
    never show anything the pipeline itself had registered
    (TradingPipeline.__post_init__ registers "fundamental_inflection")."""
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/features")
    assert resp.status_code == 200
    body = resp.json()
    assert "fundamental_inflection" in body["SHADOW"]


def test_an_explicitly_passed_feature_store_is_still_honored(api_pipeline):
    """The pipeline's own store is only the DEFAULT — an explicit
    feature_store argument (e.g. a test double) must still win, mirroring
    cost_engine's own explicit-argument precedence."""
    from services.feature_manager.store import FeatureStatus, FeatureStore

    explicit = FeatureStore()
    explicit.register("some_other_feature", purpose="test", status=FeatureStatus.ACTIVE)
    client = TestClient(create_app(api_pipeline, feature_store=explicit))
    resp = client.get("/features")
    body = resp.json()
    assert "some_other_feature" in body["ACTIVE"]
    assert "fundamental_inflection" not in body["SHADOW"]


# --- /opportunities (research-instruction §20/§94) -------------------------

def test_opportunities_endpoint_exposes_which_research_signals_contributed(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert body, "no opportunities recorded for a session that ran"
    row = body[0]
    assert {"symbol", "decision", "confidence", "thesis", "regime",
           "alpha_scores", "news_signals", "institutional_signals"} <= row.keys()
    # Whichever symbols actually survived scanning that session, at least
    # one must carry a fundamental_inflection score (established elsewhere,
    # test_security_review_regressions.py, that this happens for SOME
    # candidate at this SESSION_TIME — which specific symbol scans that
    # day is scanner-dependent, not something this endpoint test should
    # hardcode).
    assert any("fundamental_inflection" in o["alpha_scores"] for o in body)


def test_opportunities_endpoint_covers_no_trade_candidates_not_just_buys(api_pipeline):
    """Unlike /final-trade-theses (BUY survivors only), /opportunities must
    also show candidates that ended NO_TRADE/AVOID/WAIT — that's the whole
    point of exposing "why" for a symbol nothing was ordered on. Injects a
    synthetic NO_TRADE snapshot directly (real sessions at a fixed date can
    end up all-BUY or all-something-else depending on scanner output that
    day, which this endpoint's contract shouldn't depend on)."""
    from services.pdca.decision_quality import DecisionKind, DecisionSnapshot

    api_pipeline.decision_quality.record(DecisionSnapshot(
        decision_id="SYNTH-NO-TRADE", symbol="SYNTHCO", ts=api_pipeline.clock.now(),
        reference_price=50.0, decision=DecisionKind.NO_TRADE, confidence=0.4,
        expected_horizon="1w", expected_return_range=(-0.02, 0.02)))

    client = TestClient(create_app(api_pipeline))
    body = client.get("/opportunities").json()
    row = next((o for o in body if o["symbol"] == "SYNTHCO"), None)
    assert row is not None, "a NO_TRADE decision must still appear in /opportunities"
    assert row["decision"] == "NO_TRADE"


def test_opportunities_endpoint_is_empty_when_no_session_ran():
    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    client = TestClient(create_app(pipeline))
    resp = client.get("/opportunities")
    assert resp.status_code == 200
    assert resp.json() == []


def test_opportunities_endpoint_is_read_only(api_pipeline):
    client = TestClient(create_app(api_pipeline))
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)("/opportunities")
        assert resp.status_code == 405


# --- optional `lock` (scripts/run_live_dashboard.py) -----------------------

def test_without_a_lock_requests_are_not_serialized_against_a_holder():
    """Baseline: confirms the test below actually exercises the lock, not
    some other source of serialization (e.g. TestClient itself)."""
    import threading

    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    client = TestClient(create_app(pipeline))

    held = threading.Event()
    release = threading.Event()
    external_lock = threading.Lock()

    def hold():
        with external_lock:
            held.set()
            release.wait(timeout=2)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(timeout=2)
    resp = client.get("/health")   # no `lock` passed to create_app — must not block
    assert resp.status_code == 200
    release.set()
    t.join()


def test_a_request_waits_for_an_in_progress_write_to_finish():
    """A caller running pipeline.run_session() from a background thread
    (scripts/run_live_dashboard.py) passes its own lock; a request arriving
    mid-session must wait rather than read a half-updated pipeline."""
    import threading

    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    lock = threading.Lock()
    client = TestClient(create_app(pipeline, lock=lock))

    order = []
    holder_has_lock = threading.Event()
    release_holder = threading.Event()

    def hold():
        with lock:
            order.append("writer-acquired")
            holder_has_lock.set()
            release_holder.wait(timeout=2)
            order.append("writer-released")

    t = threading.Thread(target=hold)
    t.start()
    holder_has_lock.wait(timeout=2)

    def do_request():
        client.get("/health")
        order.append("request-completed")

    req_thread = threading.Thread(target=do_request)
    req_thread.start()
    req_thread.join(timeout=0.3)
    assert req_thread.is_alive(), "request completed before the writer released the lock"

    release_holder.set()
    t.join()
    req_thread.join(timeout=2)
    assert order == ["writer-acquired", "writer-released", "request-completed"]


def test_two_concurrent_requests_do_not_deadlock_the_event_loop():
    """The dashboard fires ~12 endpoints in one Promise.all — several
    requests' middleware run concurrently on the SAME asyncio event loop.
    A synchronous `with lock:` inside async middleware blocks that thread:
    the request holding the lock can't be resumed to release it, because
    resuming it needs the very event loop thread a second request's
    blocked acquire has frozen. Reproduced live with curl against a real
    uvicorn server (requests hung indefinitely on the buggy version) —
    starlette's TestClient does not reproduce it (each call appears to get
    its own portal/event loop), so this spins a REAL server on a real
    socket, the only harness that actually exercises the failure mode."""
    import socket
    import threading
    import time as _time

    import httpx
    import uvicorn

    clock = FrozenClock(current=SESSION_TIME)
    pipeline = build_pipeline(clock, _universe())
    lock = threading.Lock()
    app = create_app(pipeline, lock=lock)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            _time.sleep(0.05)
        assert server.started, "test server never started"

        holder_has_lock = threading.Event()
        release_holder = threading.Event()

        def hold():
            with lock:
                holder_has_lock.set()
                release_holder.wait(timeout=5)

        holder = threading.Thread(target=hold)
        holder.start()
        holder_has_lock.wait(timeout=2)

        results: list[int] = []

        def request(path):
            resp = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=5.0)
            results.append(resp.status_code)

        # two, matching the smallest case that reproduced the deadlock live
        requesters = [threading.Thread(target=request, args=(p,))
                     for p in ("/health", "/portfolio")]
        for t in requesters:
            t.start()
        _time.sleep(0.2)  # let both enter the middleware and block on the lock
        release_holder.set()
        holder.join(timeout=2)

        for t in requesters:
            t.join(timeout=5)
            assert not t.is_alive(), "a request never completed — event loop deadlocked"
        assert results == [200, 200]
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


def test_opportunities_survive_the_clock_advancing_past_the_recorded_session(api_pipeline):
    """scripts/run_live_dashboard.py calls app.state.record_session() and
    THEN advances the clock to the next trading day, so by the time any
    HTTP request arrives pipeline.clock.now() no longer equals the session
    that was just recorded. /opportunities must still show it — filtering
    live against pipeline.clock.now() would show "no candidates" forever
    after the very first session in that mode."""
    from datetime import timedelta

    app = create_app(api_pipeline)
    client = TestClient(app)
    app.state.record_session(None)   # what run_live_dashboard.py does post-session
    before = client.get("/opportunities").json()
    assert before, "sanity check: the fixture's session must have real opportunities"

    api_pipeline.clock.current = api_pipeline.clock.current + timedelta(days=1)
    after = client.get("/opportunities").json()
    assert after == before, "advancing the clock past the session lost its opportunities"
