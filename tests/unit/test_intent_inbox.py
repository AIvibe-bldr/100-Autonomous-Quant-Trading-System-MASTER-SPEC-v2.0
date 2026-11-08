"""Human intent inbox (§76-79): an intent is a wish, shown an analysis, confirmed, then offered."""
from datetime import datetime, timedelta, timezone

import pytest

from services.decision.human_intent import IntentAction
from services.decision.intent_inbox import (
    CONFIRMED_VALID_FOR,
    MAX_OPEN_INTENTS,
    IntentError,
    IntentInbox,
    IntentInboxUnreadable,
    IntentStatus,
)

T = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
ANALYSIS = {"symbol": "AAPL", "current_price": 100.0}


def _inbox(tmp_path):
    return IntentInbox(tmp_path / "intents.jsonl")


def _analyzed(box, symbol="AAPL"):
    iid = box.submit(symbol, IntentAction.WANT_BUY, "earnings run-up", "alice", T)
    box.record_analysis(iid, ANALYSIS, T)
    return iid


def test_the_whole_life_of_an_intent_is_recorded_with_named_operators(tmp_path):
    box = _inbox(tmp_path)
    iid = _analyzed(box)
    box.confirm(iid, "alice", T + timedelta(minutes=5))
    r = box.get(iid)
    assert r.status is IntentStatus.CONFIRMED and r.confirmed_by == "alice" and r.analysis == ANALYSIS
    assert [x.intent_id for x in box.confirmed()] == [iid]
    box.consume(iid, "ORDER_PLACED", T + timedelta(hours=1))
    r = box.get(iid)
    assert r.status is IntentStatus.CONSUMED and r.outcome == "ORDER_PLACED" and not r.is_open
    assert any("submitted by alice" in h for h in r.history) and any("confirmed by alice" in h for h in r.history)


def test_confirmation_needs_the_analysis_first(tmp_path):
    box = _inbox(tmp_path)
    iid = box.submit("AAPL", IntentAction.WANT_BUY, "why", "alice", T)
    with pytest.raises(IntentError, match="analysis"):
        box.confirm(iid, "alice", T)
    box.record_analysis(iid, ANALYSIS, T)
    with pytest.raises(IntentError):
        box.confirm(iid, "  ", T)                                           # a named operator
    box.confirm(iid, "alice", T)
    with pytest.raises(IntentError):
        box.confirm(iid, "alice", T)                                        # not twice


@pytest.mark.parametrize("kwargs,match", [
    (dict(symbol="", action=IntentAction.WANT_BUY, reason="r", operator="a"), "symbol"),
    (dict(symbol="aapl; rm", action=IntentAction.WANT_BUY, reason="r", operator="a"), "valid ticker"),
    (dict(symbol="AAPL", action=IntentAction.WANT_BUY, reason=" ", operator="a"), "reason"),
    (dict(symbol="AAPL", action=IntentAction.WANT_BUY, reason="r", operator=""), "operator"),
    (dict(symbol="AAPL", action=IntentAction.WANT_SELL, reason="r", operator="a"), "only WANT_BUY"),
])
def test_bad_or_unsupported_requests_are_refused(tmp_path, kwargs, match):
    with pytest.raises(IntentError, match=match):
        _inbox(tmp_path).submit(at=T, **kwargs)


def test_one_open_intent_per_symbol_and_a_cap_on_open_intents(tmp_path):
    box = _inbox(tmp_path)
    box.submit("AAPL", IntentAction.WANT_BUY, "r", "a", T)
    with pytest.raises(IntentError, match="already open"):
        box.submit("aapl", IntentAction.WANT_BUY, "again", "a", T)
    for i in range(MAX_OPEN_INTENTS - 1):
        box.submit(f"T{i}", IntentAction.WANT_BUY, "r", "a", T)
    with pytest.raises(IntentError, match="already open"):
        box.submit("ZZZ", IntentAction.WANT_BUY, "r", "a", T)


def test_a_cancelled_intent_is_closed_and_frees_the_symbol(tmp_path):
    box = _inbox(tmp_path)
    iid = box.submit("AAPL", IntentAction.WANT_BUY, "r", "a", T)
    with pytest.raises(IntentError):
        box.cancel(iid, "bob", "  ", T)
    box.cancel(iid, "bob", "changed my mind", T)
    assert box.get(iid).status is IntentStatus.CANCELLED and "bob" in box.get(iid).outcome
    box.submit("AAPL", IntentAction.WANT_BUY, "new wish", "a", T)
    with pytest.raises(IntentError):
        box.cancel(iid, "bob", "again", T)


def test_old_intents_expire_a_confirmation_is_short_lived(tmp_path):
    box = _inbox(tmp_path)
    iid = _analyzed(box)
    box.confirm(iid, "alice", T)
    assert box.expire_stale(T + CONFIRMED_VALID_FOR) == []                            # exactly at the limit
    assert box.expire_stale(T + CONFIRMED_VALID_FOR + timedelta(seconds=1)) == [iid]
    assert box.get(iid).status is IntentStatus.EXPIRED
    old = box.submit("MSFT", IntentAction.WANT_BUY, "r", "a", T)
    assert box.expire_stale(T + timedelta(days=8)) == [old]


def test_an_unreadable_log_makes_the_inbox_unusable_instead_of_empty(tmp_path):
    p = tmp_path / "intents.jsonl"
    p.write_text("{not json\n")
    with pytest.raises(IntentInboxUnreadable):
        IntentInbox(p).confirmed()
    p.write_text('{"event": "confirmed", "intent_id": "ghost", "operator": "x", "at": "2026-08-11T00:00:00+00:00"}\n')
    with pytest.raises(IntentInboxUnreadable):                                        # event for an unknown intent
        IntentInbox(p).records()


def test_a_missing_log_is_simply_empty(tmp_path):
    assert _inbox(tmp_path).records() == []


# ---- the CLI ----

def _cli():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "human_intent.py"
    spec = importlib.util.spec_from_file_location("human_intent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_walks_the_steps_and_refuses_what_the_inbox_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUANT_INTENT_LOG", str(tmp_path / "i.jsonl"))
    cli = _cli()
    assert cli.main(["submit", "nvda", "--operator", "alice", "--reason", "setup"]) == 0
    iid = IntentInbox(tmp_path / "i.jsonl").records()[0].intent_id
    assert "a wish, not an order" in capsys.readouterr().out
    assert cli.main(["confirm", iid, "--operator", "alice"]) == 2                  # nothing to read yet
    assert "analysis" in capsys.readouterr().err
    assert cli.main(["submit", "AAPL", "--operator", "alice", "--reason", "x", "--action", "WANT_SELL"]) == 2
    assert "only WANT_BUY" in capsys.readouterr().err
    IntentInbox(tmp_path / "i.jsonl").record_analysis(iid, {"symbol": "NVDA"}, T)
    assert cli.main(["show", iid]) == 0 and '"symbol": "NVDA"' in capsys.readouterr().out
    assert cli.main(["confirm", iid, "--operator", "alice"]) == 0
    assert cli.main(["list"]) == 0 and "CONFIRMED" in capsys.readouterr().out
    assert cli.main(["cancel", iid, "--operator", "bob", "--reason", "changed my mind"]) == 0
