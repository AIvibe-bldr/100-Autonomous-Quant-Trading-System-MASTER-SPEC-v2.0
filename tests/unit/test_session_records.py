"""Multi-day session records: store, recorder, summaries, report script."""
import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from packages.common.alerts import AlertSeverity
from packages.common.calendar import TradingCalendar
from packages.common.clock import FrozenClock
from packages.common.durable_store import DurableSessionStore, SessionRecord
from packages.common.environment import Environment
from services.market_data.universe import UniverseManager, UniverseSymbol
from services.supervisor.heartbeat import HeartbeatRegistry
from services.supervisor.performance_report import PROPOSED_MIN_SESSIONS, render, summarize
from services.supervisor.runner import RunnerConfig, SessionRunner
from services.supervisor.session_record import SessionRecorder
from tests.conftest import SYMBOLS, build_pipeline


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def _rec(day, equity, pnl, dd=0.0, state="NORMAL", warnings=0, tag="t", cost=None, filled=0):
    return SessionRecord(
        session_date=day, model_tag=tag, recorded_at=utc(2026, 8, 11, 15), equity=equity, cash=equity,
        high_water_mark=max(equity, 1000.0), drawdown=dd, day_pnl=pnl, realized_pnl_total=0.0,
        fees_total=1.5, scanned=20, candidates=5, decision_candidates=5, skeptic_vetoes=1,
        audit_passed=2, risk_passed=2, orders_filled=filled, stops_triggered=0, open_positions=0,
        data_warnings=warnings, risk_state=state, llm_cost_usd=cost, detail_json="{}")


# ---- store ----

def test_store_roundtrips_orders_by_date_and_rejects_a_second_record_for_a_date(tmp_path):
    st = DurableSessionStore(str(tmp_path / "s.db"), Environment.PAPER)
    st.record(_rec("2026-08-12", 1010.0, 10.0))
    st.record(_rec("2026-08-11", 1000.0, 0.0, cost=0.25))
    assert [r.session_date for r in st.all()] == ["2026-08-11", "2026-08-12"]
    assert st.latest_equity() == 1010.0 and st.all()[0].llm_cost_usd == 0.25
    with pytest.raises(ValueError, match="already recorded"):
        st.record(_rec("2026-08-12", 5.0, -1005.0))
    assert [r.equity for r in st.all()] == [1000.0, 1010.0]               # history untouched


def test_environments_never_share_rows_in_one_file(tmp_path):
    path = str(tmp_path / "s.db")
    DurableSessionStore(path, Environment.PAPER).record(_rec("2026-08-11", 1000.0, 0.0))
    assert DurableSessionStore(path, Environment.LIVE).all() == []


# ---- recorder, driven by the real runner over a week ----

def _week(tmp_path, tag="mock-ai"):
    clock = FrozenClock(current=utc(2026, 8, 11, 12))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    pipeline = build_pipeline(clock, universe, initial_cash=670.0)
    store = DurableSessionStore(str(tmp_path / "s.db"), Environment.PAPER)
    recorder = SessionRecorder(pipeline, store, tag)

    def after(at, result):
        recorder(at, result)
        pipeline.execution.broker.settle()
    sent = []
    sink = type("S", (), {"send": lambda self, sev, msg: sent.append((sev, msg))})()
    runner = SessionRunner(pipeline, TradingCalendar(), clock, HeartbeatRegistry(clock), sink,
                           RunnerConfig(), sleep=lambda s: clock.advance(s), after_session=after)
    runner.run_forever(lambda: clock.now() >= utc(2026, 8, 15, 0))
    return pipeline, store, sent


def test_a_week_of_sessions_is_recorded_and_the_numbers_reconcile(tmp_path):
    pipeline, store, sent = _week(tmp_path)
    recs = store.all()
    assert [r.session_date for r in recs] == ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    assert all(r.model_tag == "mock-ai" for r in recs)
    # the daily P&L chain adds up to the equity change since the initial cash
    assert sum(r.day_pnl for r in recs) == pytest.approx(recs[-1].equity - 670.0)
    assert recs[-1].fees_total == pytest.approx(pipeline.ledger.fees_paid)
    assert all(0.0 <= r.drawdown <= 1.0 and r.high_water_mark >= r.equity - 1e-9 for r in recs)
    assert sent == []


def test_recorder_needs_a_model_tag(tmp_path):
    with pytest.raises(ValueError):
        SessionRecorder(None, None, "  ")


def test_a_recording_failure_stops_the_runner_like_any_other_failure(tmp_path):
    clock = FrozenClock(current=utc(2026, 8, 11, 15))
    universe = UniverseManager()
    for s in SYMBOLS:
        universe.add(UniverseSymbol(symbol=s, listed_from=date(2015, 1, 1)))
    pipeline = build_pipeline(clock, universe)
    sent = []
    sink = type("S", (), {"send": lambda self, sev, msg: sent.append((sev, msg))})()

    def broken(at, result):
        raise OSError("disk full")
    runner = SessionRunner(pipeline, TradingCalendar(), clock, HeartbeatRegistry(clock), sink,
                           RunnerConfig(), sleep=lambda s: None, after_session=broken)
    reason = runner.run_forever(lambda: False)
    assert "after-session step failed" in reason and "disk full" in reason
    assert any(sev is AlertSeverity.CRITICAL for sev, _ in sent)


# ---- summaries ----

def test_summary_numbers_and_the_small_sample_warning():
    recs = [_rec("2026-08-11", 1010.0, 10.0, filled=1), _rec("2026-08-12", 990.0, -20.0, dd=0.0198,
                                                                 state="HALT_NEW_ENTRIES", warnings=2),
            _rec("2026-08-13", 990.0, 0.0, dd=0.0198, cost=0.5)]
    s = summarize(recs)
    assert s.sessions == 3 and s.start_equity == pytest.approx(1000.0)
    assert s.total_return == pytest.approx(-0.01) and s.max_drawdown == pytest.approx(0.0198)
    assert (s.up_days, s.down_days, s.flat_days) == (1, 1, 1)
    assert (s.best_day, s.worst_day) == (10.0, -20.0)
    assert s.non_normal_risk_days == 1 and s.warning_days == 1 and s.orders_filled == 1
    assert s.llm_cost_usd == 0.5 and not s.enough_sessions
    out = render([s])
    assert "3 sessions" in out and f"3/{PROPOSED_MIN_SESSIONS}" in out and "too few" in out


def test_comparing_runs_says_a_difference_is_not_evidence():
    a = summarize([_rec("2026-08-11", 1010.0, 10.0, tag="A")])
    b = summarize([_rec("2026-08-11", 990.0, -10.0, tag="B")])
    out = render([a, b])
    assert "[A]" in out and "[B]" in out and "not evidence" in out


def test_enough_sessions_removes_the_small_sample_warning():
    recs = [_rec(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 1000.0, 0.0) for i in range(PROPOSED_MIN_SESSIONS)]
    assert summarize(recs).enough_sessions and "too few" not in render([summarize(recs)])


def test_empty_history_cannot_be_summarised():
    with pytest.raises(ValueError):
        summarize([])


# ---- scripts ----

def _script(name):
    path = Path(__file__).resolve().parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_script_prints_a_summary_and_daily_rows(tmp_path, capsys):
    _week(tmp_path)
    assert _script("report_performance.py").main([str(tmp_path / "s.db"), "--daily"]) == 0
    out = capsys.readouterr().out
    assert "2026-08-11" in out and "(4 sessions)" in out and "too few" in out


def test_report_script_fails_clearly_on_missing_or_empty_input(tmp_path, capsys):
    mod = _script("report_performance.py")
    assert mod.main([str(tmp_path / "nope.db")]) == 2
    DurableSessionStore(str(tmp_path / "empty.db"), Environment.PAPER)
    assert mod.main([str(tmp_path / "empty.db")]) == 2


def test_runner_script_refuses_a_state_dir_that_already_holds_sessions(tmp_path, capsys):
    st = DurableSessionStore(str(tmp_path / "sessions.db"), Environment.PAPER)
    st.record(_rec("2026-08-11", 1000.0, 0.0))
    assert _script("run_paper_runner.py").main(["--state-dir", str(tmp_path)]) == 2
    assert "refusing to start" in capsys.readouterr().err
