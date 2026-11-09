"""Ops dashboard: read-only state, honest errors, auth, and a safe page."""
import hashlib
import importlib.util
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.ops import OPS_HTML, create_ops_app
from packages.common.alert_sinks import FileAlertSink
from packages.common.alerts import AlertSeverity
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.durable_store import DurableSessionStore
from packages.common.environment import Environment
from services.composition import attach_learning_observers
from services.decision.human_intent import IntentAction
from services.decision.intent_inbox import IntentInbox
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.incident_log import IncidentLog
from services.supervisor.ops_state import OpsState
from services.supervisor.recovery import FaultSeverity, RecoveryManager
from services.supervisor.runner import RunnerConfig, SessionRunner
from services.supervisor.session_record import SessionRecorder
from services.supervisor.status_file import FILE_NAME, write_status
from tests.conftest import SYMBOLS, build_pipeline

START = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 14, 22, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def state(tmp_path_factory):
    """A real state directory: a runner with all observers ran four days and left its files behind."""
    d = tmp_path_factory.mktemp("state")
    clock = FrozenClock(current=START)
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    box = IntentInbox(d / "intents.jsonl")
    box.submit("NVDA", IntentAction.WANT_BUY, "<img src=x onerror=alert(1)> my reason", "alice", START)
    p = build_pipeline(clock, universe, initial_cash=670.0, human_inbox=box)
    attach_learning_observers(p, 670.0)
    store = DurableSessionStore(str(d / "sessions.db"), Environment.PAPER)
    recorder = SessionRecorder(p, store, "mock-ai")
    log = IncidentLog(d / "incidents.jsonl")
    sink = FileAlertSink(d / "alerts.jsonl")
    runner = SessionRunner(
        p, TradingCalendar(), clock, HeartbeatRegistry(clock), sink, RunnerConfig(), sleep=lambda s: clock.advance(s),
        after_session=lambda at, r: (recorder(at, r), p.execution.broker.settle()),
        status_sink=lambda st: write_status(d / FILE_NAME, st), market_open_hook=p.analyze_human_intents,
        recovery=RecoveryManager(clock), incident_log=log)
    runner.run_forever(lambda: clock.now() >= END)
    inc = RecoveryManager(clock, start_seq=log.next_seq()).report_fault("runner", "boom <b>bold</b>", FaultSeverity.MAJOR)
    log.record_fault(inc, {})
    sink.send(AlertSeverity.CRITICAL, "an alert with <script>x</script>")
    return d, clock.now()


def ops(state, tmp_path):
    d, _ = state
    return OpsState(d, incidents=IncidentLog(d / "incidents.jsonl"), intents=IntentInbox(d / "intents.jsonl"),
                    kill_switch=__import__("packages.common.kill_switch", fromlist=["KillSwitch"]).KillSwitch(tmp_path / "ks.json"),
                    live_gate=__import__("packages.common.live_gate", fromlist=["LiveGate"]).LiveGate(tmp_path / "lg.json"))


def test_performance_carries_the_series_and_the_small_sample_warning(state, tmp_path):
    p = ops(state, tmp_path).performance()
    assert p["sessions"] == 4 and len(p["series"]) == 4 and not p["enough_sessions"]
    assert "too few" in p["warning"] and p["summary"]["start_equity"] == pytest.approx(670.0)


def test_sessions_are_newest_first_and_detail_includes_the_observers(state, tmp_path):
    o = ops(state, tmp_path)
    s = o.sessions(10)
    assert [r["session_date"] for r in s["sessions"]] == ["2026-08-14", "2026-08-13", "2026-08-12", "2026-08-11"]
    assert "detail_json" not in s["sessions"][0] and s["total"] == 4
    d = o.session_detail("2026-08-12")
    assert set(d["detail"]["observers"]) >= {"forecast", "shadow", "capital_cells"} and o.session_detail("2030-01-01") is None
    latest = o.observers()
    assert latest["session_date"] == "2026-08-14" and "no_trade_reasons" in latest


def test_status_says_ok_while_fresh_and_critical_once_the_heartbeat_is_stale(state, tmp_path):
    d, now = state
    o = ops(state, tmp_path)
    fresh = o.status(now + timedelta(seconds=30))
    assert fresh["runner"]["state"] == "STOPPED" and fresh["runner"]["sessions_run"] == 4
    codes = {f["code"] for f in fresh["findings"]}
    assert "open_incident" in codes and "runner_stopped" in codes and fresh["overall"] == "CRITICAL"
    assert fresh["open_major_incidents"] and fresh["kill_switch"]["engaged"] is False


def test_incidents_intents_and_alerts_are_listed(state, tmp_path):
    o = ops(state, tmp_path)
    assert [i["service"] for i in o.incidents()["incidents"]][0] == "runner"
    intents = o.intents()["intents"]
    assert intents[0]["symbol"] == "NVDA" and intents[0]["analysis"]                      # analysed by the runner
    assert o.alerts()["alerts"][0]["severity"] == "CRITICAL"


def test_reading_never_changes_a_file(state, tmp_path):
    d, now = state
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()}
    o = ops(state, tmp_path)
    for fn in (o.performance, o.observers, o.incidents, o.intents, o.alerts, lambda: o.status(now), lambda: o.sessions(5)):
        fn()
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()} == before


def test_the_session_store_opened_read_only_refuses_to_write_and_to_create(state, tmp_path):
    d, _ = state
    ro = DurableSessionStore(str(d / "sessions.db"), Environment.PAPER, read_only=True)
    with pytest.raises(PermissionError):
        ro.record(ro.all()[0])
    with pytest.raises(Exception):                                                          # a missing file is not created
        DurableSessionStore(str(tmp_path / "absent.db"), Environment.PAPER, read_only=True)
    assert not (tmp_path / "absent.db").exists()


def test_an_unreadable_database_is_an_error_not_an_empty_dashboard(tmp_path):
    (tmp_path / "sessions.db").write_bytes(b"this is not a sqlite file at all, just text" * 10)
    o = OpsState(tmp_path, incidents=IncidentLog(tmp_path / "i"), intents=IntentInbox(tmp_path / "n"))
    assert "unreadable" in o.performance()["error"] and "unreadable" in o.sessions()["error"]
    assert "unreadable" in o.observers()["error"]


def test_an_empty_state_dir_is_no_data_not_a_crash(tmp_path):
    o = OpsState(tmp_path, incidents=IncidentLog(tmp_path / "i"), intents=IntentInbox(tmp_path / "n"))
    assert o.performance()["sessions"] == 0 and o.sessions()["sessions"] == []
    s = o.status(START)
    assert s["runner"] is None and {f["code"] for f in s["findings"]} == {"no_status"}


def test_unreadable_operator_files_are_reported_in_their_own_section(tmp_path):
    (tmp_path / "i").write_text("{garbage\n")
    (tmp_path / "n").write_text("{garbage\n")
    o = OpsState(tmp_path, incidents=IncidentLog(tmp_path / "i"), intents=IntentInbox(tmp_path / "n"))
    assert o.intents()["error"] and o.incidents()["incidents"][0]["incident_id"] == "LOG-UNREADABLE"
    assert "LOG-UNREADABLE" in o.status(START)["open_major_incidents"]


# ---- the API ----

def client(state, tmp_path, token=None):
    d, now = state
    return TestClient(create_ops_app(ops(state, tmp_path), token=token, now=lambda: now))


def test_every_route_is_a_get_and_nothing_can_write(state, tmp_path):
    app = create_ops_app(ops(state, tmp_path))
    methods = {m for r in app.routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "HEAD"}
    c = TestClient(app)
    for verb in ("post", "put", "delete", "patch"):
        assert getattr(c, verb)("/api/status").status_code in (404, 405)
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_endpoints_answer_and_validate_input(state, tmp_path):
    c = client(state, tmp_path)
    for path in ("/api/status", "/api/performance", "/api/sessions?limit=5", "/api/observers", "/api/incidents",
                 "/api/intents", "/api/alerts"):
        assert c.get(path).status_code == 200, path
    assert c.get("/api/sessions/2026-08-12").json()["session_date"] == "2026-08-12"
    assert c.get("/api/sessions/2030-01-01").status_code == 404
    assert c.get("/api/sessions/../../etc/passwd").status_code in (400, 404)
    assert c.get("/api/sessions/2026-08-12'; DROP TABLE x;--").status_code == 400
    assert c.get("/api/sessions?limit=0").status_code == 422 and c.get("/api/sessions?limit=9999").status_code == 422


def test_responses_carry_security_headers(state, tmp_path):
    r = client(state, tmp_path).get("/")
    assert r.status_code == 200 and "運用ダッシュボード" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"] and "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_token_protects_every_route_and_accepts_bearer_or_query(state, tmp_path):
    c = client(state, tmp_path, token="s3cret")
    for path in ("/", "/api/status", "/api/performance", "/api/intents"):
        assert c.get(path).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/api/status?token=s3cret").status_code == 200 and c.get("/?token=s3cret").status_code == 200


# ---- the page and the script ----

def test_the_page_never_puts_data_into_html():
    html = OPS_HTML.read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in html, banned
    assert len(re.findall(r"<script", html)) == 1 and "http://" not in html and "https://" not in html   # no external loads


def test_the_script_refuses_a_public_bind_without_a_token_and_a_missing_directory(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_ops_dashboard.py"
    spec = importlib.util.spec_from_file_location("run_ops_dashboard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.delenv("QUANT_OPS_TOKEN", raising=False)
    assert mod.main(["--state-dir", str(tmp_path), "--host", "0.0.0.0"]) == 2
    assert "QUANT_OPS_TOKEN" in capsys.readouterr().err
    assert mod.main(["--state-dir", str(tmp_path / "nope")]) == 2


def test_the_runbook_only_names_scripts_and_variables_that_exist():
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "operations.md").read_text()
    for name in set(re.findall(r"scripts/([a-z_]+\.py)", doc)):
        assert (root / "scripts" / name).exists(), name
    sources = "\n".join(p.read_text() for d in ("packages", "services", "scripts", "apps")
                        for p in (root / d).rglob("*.py"))
    for var in set(re.findall(r"`(QUANT_[A-Z_]+)`", doc)):
        assert var in sources, var                      # every variable the runbook names is read by some code


def test_status_reports_when_the_account_was_last_saved(tmp_path):
    from packages.common.durable_store import DurableCheckpointStore
    o = OpsState(tmp_path, incidents=IncidentLog(tmp_path / "i"), intents=IntentInbox(tmp_path / "n"))
    assert o.status(START)["checkpoint"] is None
    DurableCheckpointStore(str(tmp_path / "checkpoints.db"), Environment.PAPER).save({"x": 1}, "session_end:2026-08-11", START)
    assert o.status(START)["checkpoint"]["reason"] == "session_end:2026-08-11"
