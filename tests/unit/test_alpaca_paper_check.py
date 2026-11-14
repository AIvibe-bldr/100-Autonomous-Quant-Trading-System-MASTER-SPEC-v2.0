"""scripts/alpaca_paper_check.py against a stateful fake of Alpaca's paper API (no network)."""
import importlib.util
import json
from pathlib import Path

import pytest

from packages.broker_adapters.alpaca import KEY_ENV, SECRET_ENV
from tests.unit.test_alpaca_broker import KEY, SECRET, FakeAlpaca

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "alpaca_paper_check.py"


def load_script():
    spec = importlib.util.spec_from_file_location("alpaca_paper_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.sleep = lambda s: None
    return mod


class Market:
    """Just enough of Alpaca's paper API for the check script."""

    def __init__(self, *, is_open=True, config_sticks=True, fractional_gtc_stop=True, status_code=None):
        self.orders, self.activities, self.positions = {}, [], {}
        self.cash, self.price, self.is_open = 1000.0, 50.0, is_open
        self.config = {"max_margin_multiplier": "2", "no_shorting": False}
        self.config_sticks, self.fractional_gtc_stop, self.status_code = config_sticks, fractional_gtc_stop, status_code

    def order_json(self, o):
        return {"id": o["id"], "client_order_id": o["cid"], "status": o["status"], "qty": str(o["qty"]),
                "filled_qty": str(o["filled"]), "created_at": "2026-11-13T15:00:00.123456789Z", "symbol": o["symbol"]}

    def __call__(self, method, path, query, body):
        if self.status_code:
            return self.status_code, {"message": "unauthorized"}
        if (method, path) == ("GET", "/v2/account"):
            return 200, {"status": "ACTIVE", "currency": "USD", "cash": str(self.cash), "equity": str(self.cash),
                         "buying_power": str(self.cash), "multiplier": self.config["max_margin_multiplier"],
                         "pattern_day_trader": False, "daytrade_count": 0, "trading_blocked": False}
        if (method, path) == ("GET", "/v2/account/configurations"):
            return 200, dict(self.config)
        if (method, path) == ("PATCH", "/v2/account/configurations"):
            if self.config_sticks:
                self.config.update(body)
            return 200, dict(self.config)
        if (method, path) == ("GET", "/v2/clock"):
            return 200, {"is_open": self.is_open, "next_open": "2026-11-16T14:30:00Z", "next_close": "2026-11-16T21:00:00Z"}
        if (method, path) == ("GET", "/v2/positions"):
            return 200, [{"symbol": s, "qty": str(q), "avg_entry_price": str(self.price)} for s, q in self.positions.items() if q > 0]
        if (method, path) == ("GET", "/v2/orders"):
            return 200, [self.order_json(o) for o in self.orders.values() if o["status"] == "new"]
        if (method, path) == ("POST", "/v2/orders"):
            return self.place(body)
        if (method, path) == ("GET", "/v2/orders:by_client_order_id"):
            o = self.orders.get(query["client_order_id"])
            return (200, self.order_json(o)) if o else (404, {"message": "order not found"})
        if method == "DELETE" and path.startswith("/v2/orders/"):
            for o in self.orders.values():
                if o["id"] == path.rsplit("/", 1)[1]:
                    o["status"] = "canceled"
                    return 204, None
            return 404, {"message": "order not found"}
        if (method, path) == ("GET", "/v2/account/activities/FILL"):
            return 200, self.activities
        return None

    def place(self, body):
        qty, symbol = float(body["qty"]), body["symbol"]
        o = {"id": f"ord-{len(self.orders) + 1}", "cid": body["client_order_id"], "symbol": symbol, "qty": qty,
             "filled": 0.0, "status": "new", "type": body["type"], "side": body["side"]}
        if body["type"] == "stop" and qty != int(qty) and body["time_in_force"] == "gtc" and not self.fractional_gtc_stop:
            return 422, {"message": "fractional orders must be day orders"}
        if body["side"] == "sell" and self.positions.get(symbol, 0.0) < qty - 1e-9:
            return 403, {"message": "insufficient qty available for order"}
        if body["type"] == "market":
            o["status"], o["filled"] = "filled", qty
            sign = 1 if body["side"] == "buy" else -1
            self.positions[symbol] = self.positions.get(symbol, 0.0) + sign * qty
            self.cash -= sign * qty * self.price
            self.activities.append({"id": f"act-{len(self.activities) + 1}", "order_id": o["id"], "symbol": symbol,
                                    "side": body["side"], "qty": str(qty), "price": str(self.price),
                                    "transaction_time": "2026-11-13T15:00:05.123456789Z", "type": "fill"})
        self.orders[body["client_order_id"]] = o
        return 200, self.order_json(o)


@pytest.fixture
def run(monkeypatch, tmp_path):
    fakes = []

    def go(*args, market=None):
        fake = FakeAlpaca()
        fakes.append(fake)
        fake.dynamic = market or Market()
        monkeypatch.setenv(KEY_ENV, KEY)
        monkeypatch.setenv(SECRET_ENV, SECRET)
        mod = load_script()
        code = mod.main([*args, "--base-url", fake.url, "--state-dir", str(tmp_path / "st")])
        return code, fake, fake.dynamic

    yield go
    for f in fakes:
        f.close()


def writes(fake):
    return [(r["method"], r["path"]) for r in fake.requests if r["method"] in ("POST", "PATCH", "DELETE")]


def test_the_default_run_only_reads_and_reports_that_the_account_is_not_cash_style(run, capsys):
    code, fake, _ = run()
    out = capsys.readouterr().out
    assert code == 0 and not writes(fake)
    assert "PAPER" in out and "設定の検証: NG" in out and "max_margin_multiplier" in out
    assert KEY not in out and SECRET not in out


def test_set_cash_config_writes_once_and_reads_it_back(run, capsys):
    code, fake, market = run("--set-cash-config")
    out = capsys.readouterr().out
    assert code == 0 and writes(fake) == [("PATCH", "/v2/account/configurations")]
    assert "読み戻し: OK" in out and market.config == {"max_margin_multiplier": "1", "no_shorting": True}


def test_a_setting_that_does_not_stick_is_reported_not_assumed(run, capsys):
    code, _, _ = run("--set-cash-config", market=Market(config_sticks=False))
    assert code == 0 and "読み戻し: NG" in capsys.readouterr().out


def test_the_fractional_check_buys_tries_a_gtc_stop_and_cancels_it(run, capsys, tmp_path):
    code, fake, market = run("--orders", "fractional")
    out = capsys.readouterr().out
    assert code == 0 and [p for m, p in writes(fake) if m == "POST"] == ["/v2/orders", "/v2/orders"]
    assert [m for m, _ in writes(fake)].count("DELETE") == 1                      # the stop is cancelled
    assert "受け付けられました" in out and "そろっています" in out and "gtc_stop_accepted: True" in out
    stop_body = [r["body"] for r in fake.requests if r["method"] == "POST"][1]
    assert stop_body["type"] == "stop" and stop_body["time_in_force"] == "gtc" and stop_body["stop_price"] == "40.00"
    assert json.loads((tmp_path / "st" / "alpaca_check.json").read_text(encoding="utf-8")) == [{"symbol": "SPY", "qty": 0.01}]
    assert KEY not in out and SECRET not in out


def test_a_refused_fractional_gtc_stop_is_reported_clearly(run, capsys):
    code, fake, _ = run("--orders", "fractional", market=Market(fractional_gtc_stop=False))
    out = capsys.readouterr().out
    assert code == 0 and "拒否されました: HTTP 422: fractional orders must be day orders" in out and "gtc_stop_accepted: False" in out
    assert "DELETE" not in [m for m, _ in writes(fake)]


def test_nothing_is_ordered_while_the_market_is_closed(run, capsys):
    code, fake, _ = run("--orders", "whole", market=Market(is_open=False))
    assert code == 0 and not writes(fake) and "閉じています" in capsys.readouterr().out


def test_cleanup_sells_what_the_check_bought_and_forgets_it(run, capsys, monkeypatch, tmp_path):
    market = Market()
    run("--orders", "whole", market=market)
    assert market.positions["F"] == 1.0
    mod_fake = FakeAlpaca()
    mod_fake.dynamic = market
    try:
        monkeypatch.setenv(KEY_ENV, KEY)
        monkeypatch.setenv(SECRET_ENV, SECRET)
        mod = load_script()
        assert mod.main(["--cleanup", "--base-url", mod_fake.url, "--state-dir", str(tmp_path / "st")]) == 0
    finally:
        mod_fake.close()
    assert market.positions["F"] == 0.0
    assert json.loads((tmp_path / "st" / "alpaca_check.json").read_text(encoding="utf-8")) == []
    assert "約定" in capsys.readouterr().out


def test_an_authentication_failure_ends_with_an_error_and_never_prints_the_keys(run, capsys):
    code, _, _ = run(market=Market(status_code=401))
    out = capsys.readouterr().out
    assert code == 1 and "エラー" in out and KEY not in out and SECRET not in out
