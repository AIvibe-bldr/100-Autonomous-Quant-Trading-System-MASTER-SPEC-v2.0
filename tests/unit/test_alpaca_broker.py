"""The Alpaca adapter against a local fake server (no network). It must behave like the PaperBroker as seen by
the engine (docs/HANDOFF_next_session.md §3b): same error contract, fail closed on anything unexpected, and
never able to reach the live endpoint."""
import json
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from packages.broker_adapters.alpaca import (
    KEY_ENV,
    PAPER_HOST,
    SECRET_ENV,
    AlpacaBroker,
    AlpacaConfigurationError,
)
from packages.broker_adapters.base import (
    BrokerAdapter,
    BrokerDisconnectedError,
    BrokerTimeoutError,
    DuplicateClientOrderIdError,
)
from packages.common.environment import Environment, require_same_environment
from packages.schemas.core import Action, BrokerOrderRequest, OrderState, OrderType, TimeInForce

KEY, SECRET = "PKTESTKEY123", "s3cret-value-xyz"


class FakeAlpaca:
    """A local HTTP server answering like Alpaca's trading API. `routes` maps (method, path) to a callable
    (query, body) -> (status, payload) or a list of such answers consumed one per request."""

    def __init__(self):
        self.requests, self.routes, self.delay = [], {}, 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def _handle(self):
                url = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append({"method": self.command, "path": url.path, "query": dict(urllib.parse.parse_qsl(url.query)),
                                       "body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if outer.delay:
                    time.sleep(outer.delay)
                answer = outer.routes.get((self.command, url.path), (404, {"message": "not found"}))
                if isinstance(answer, list):
                    answer = answer.pop(0) if len(answer) > 1 else answer[0]
                if callable(answer):
                    answer = answer(dict(urllib.parse.parse_qsl(url.query)), body)
                status, payload = answer
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = _handle

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def seen(self, method, path):
        return [r for r in self.requests if r["method"] == method and r["path"] == path]


@pytest.fixture
def fake():
    f = FakeAlpaca()
    yield f
    f.close()


def broker(fake, **kw):
    return AlpacaBroker(KEY, SECRET, base_url=fake.url, sleep=lambda s: None, **kw)


def request(**kw):
    base = dict(client_order_id="paper-abcd1234-1-aaaaaa", symbol="PLTR", side=Action.BUY, qty=2.5,
                order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY)
    base.update(kw)
    return BrokerOrderRequest(**base)


def order_json(**kw):
    o = {"id": "ord-1", "client_order_id": "paper-abcd1234-1-aaaaaa", "status": "new", "qty": "2.5",
         "filled_qty": "0", "created_at": "2026-11-13T15:00:00.123456789Z", "symbol": "PLTR"}
    o.update(kw)
    return o


# ---- construction: it can only ever be the paper endpoint ----------------------------------------------------
def test_the_environment_is_paper_and_binds_to_a_paper_engine():
    b = AlpacaBroker(KEY, SECRET)
    assert isinstance(b, BrokerAdapter) and b.environment is Environment.PAPER
    assert require_same_environment(Environment.PAPER, b.environment) is Environment.PAPER


@pytest.mark.parametrize("url", ["https://api.alpaca.markets", "https://api.alpaca.markets/v2",
                                 "https://example.com", f"http://{PAPER_HOST}", "http://10.0.0.5:8080",
                                 "https://paper-api.alpaca.markets.evil.example"])
def test_the_live_host_and_any_other_host_are_refused(url):
    with pytest.raises(ValueError, match="PAPER endpoint"):
        AlpacaBroker(KEY, SECRET, base_url=url)


def test_keys_are_required_and_read_from_the_environment_without_echoing_them():
    with pytest.raises(ValueError) as e:
        AlpacaBroker.from_env({})
    assert KEY_ENV in str(e.value) and SECRET_ENV in str(e.value)
    with pytest.raises(ValueError):
        AlpacaBroker("", SECRET)
    assert AlpacaBroker.from_env({KEY_ENV: KEY, SECRET_ENV: SECRET}).environment is Environment.PAPER


# ---- submit ---------------------------------------------------------------------------------------------------
def test_submit_sends_the_order_in_alpacas_format_with_credentials_and_its_own_user_agent(fake):
    fake.routes[("POST", "/v2/orders")] = (200, order_json())
    ack = broker(fake).submit_order(request())
    assert ack.accepted and ack.broker_order_id == "ord-1"
    (r,) = fake.seen("POST", "/v2/orders")
    assert r["body"] == {"symbol": "PLTR", "qty": "2.5", "side": "buy", "type": "market", "time_in_force": "day",
                         "client_order_id": "paper-abcd1234-1-aaaaaa"}
    assert r["headers"]["apca-api-key-id"] == KEY and r["headers"]["apca-api-secret-key"] == SECRET
    assert not r["headers"]["user-agent"].lower().startswith("python-urllib")


@pytest.mark.parametrize("qty,text", [(0.0354, "0.0354"), (3.0, "3"), (1.23456789012, "1.23456789"), (0.1 + 0.2, "0.3")])
def test_quantities_are_sent_as_plain_decimal_text(fake, qty, text):
    fake.routes[("POST", "/v2/orders")] = (200, order_json())
    broker(fake).submit_order(request(qty=qty))
    assert fake.seen("POST", "/v2/orders")[0]["body"]["qty"] == text


@pytest.mark.parametrize("side,otype,price,sent", [
    (Action.SELL, OrderType.STOP, 42.2986, "42.30"),     # a protective sell-stop rounds UP: triggers marginally earlier
    (Action.SELL, OrderType.LIMIT, 10.011, "10.02"),     # a sell never asks for less than approved
    (Action.BUY, OrderType.LIMIT, 10.019, "10.01"),      # a buy never pays more than approved
    (Action.BUY, OrderType.LIMIT, 0.45678, "0.4567"),    # under $1 the tick is 1/100 cent
    (Action.SELL, OrderType.STOP, 100.0, "100.00"),
])
def test_prices_go_out_on_the_tick_never_worse_than_approved(fake, side, otype, price, sent):
    fake.routes[("POST", "/v2/orders")] = (200, order_json())
    tif = TimeInForce.GTC if otype is OrderType.STOP else TimeInForce.DAY
    kw = {"limit_price": price} if otype is OrderType.LIMIT else {"stop_price": price}
    broker(fake).submit_order(request(side=side, order_type=otype, time_in_force=tif, **kw))
    body = fake.seen("POST", "/v2/orders")[0]["body"]
    assert body["type"] == otype.value.lower() and body["time_in_force"] == tif.value.lower()
    assert (body.get("limit_price") or body.get("stop_price")) == sent


def test_alpacas_refusals_become_a_rejected_ack_with_the_reason(fake):
    fake.routes[("POST", "/v2/orders")] = (403, {"message": "insufficient buying power"})
    ack = broker(fake).submit_order(request())
    assert not ack.accepted and "insufficient buying power" in ack.reason
    fake.routes[("POST", "/v2/orders")] = (422, {"message": "fractional orders must be DAY"})
    assert "must be DAY" in broker(fake).submit_order(request()).reason


def test_a_duplicate_client_order_id_raises_like_the_paper_broker(fake):
    fake.routes[("POST", "/v2/orders")] = (422, {"message": "client_order_id must be unique"})
    with pytest.raises(DuplicateClientOrderIdError):
        broker(fake).submit_order(request())


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_an_unknown_submit_outcome_is_a_timeout_and_is_never_retried(fake, status):
    fake.routes[("POST", "/v2/orders")] = (status, {"message": "busy"})
    with pytest.raises(BrokerTimeoutError):
        broker(fake).submit_order(request())
    assert len(fake.seen("POST", "/v2/orders")) == 1


def test_a_submit_that_times_out_is_a_timeout_not_a_rejection(fake):
    fake.routes[("POST", "/v2/orders")] = (200, order_json())
    fake.delay = 0.6
    with pytest.raises(BrokerTimeoutError):
        broker(fake, timeout=0.2).submit_order(request())


def test_bad_credentials_and_an_unreachable_broker_are_disconnects_and_never_leak_the_keys(fake):
    fake.routes[("POST", "/v2/orders")] = (401, {"message": "unauthorized"})
    with pytest.raises(BrokerDisconnectedError) as e:
        broker(fake).submit_order(request())
    assert KEY not in str(e.value) and SECRET not in str(e.value)
    fake.close()
    with pytest.raises(BrokerDisconnectedError) as e2:
        broker(fake).get_positions()
    assert KEY not in repr(e2.value) and SECRET not in repr(e2.value)


def test_a_quantity_or_price_too_small_to_send_is_rejected_locally(fake):
    ack = broker(fake).submit_order(request(qty=1e-12))
    assert not ack.accepted and not fake.requests


# ---- status / cancel ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("alpaca,state", [
    ("pending_new", OrderState.SUBMITTED), ("new", OrderState.ACKNOWLEDGED), ("accepted", OrderState.ACKNOWLEDGED),
    ("partially_filled", OrderState.PARTIALLY_FILLED), ("filled", OrderState.FILLED),
    ("pending_cancel", OrderState.CANCEL_REQUESTED), ("canceled", OrderState.CANCELLED),
    ("expired", OrderState.EXPIRED), ("rejected", OrderState.REJECTED),
    ("stopped", OrderState.UNKNOWN), ("suspended", OrderState.UNKNOWN), ("replaced", OrderState.UNKNOWN),
    ("done_for_day", OrderState.UNKNOWN), ("something_new", OrderState.UNKNOWN),
])
def test_order_status_is_mapped_and_anything_unrecognised_is_unknown(fake, alpaca, state):
    fake.routes[("GET", "/v2/orders:by_client_order_id")] = (200, order_json(status=alpaca, filled_qty="1.0"))
    st = broker(fake).get_order_status("paper-abcd1234-1-aaaaaa")
    assert st.state is state and st.filled_qty == 1.0 and st.remaining_qty == 1.5 and st.broker_order_id == "ord-1"


def test_status_of_an_unknown_order_is_a_key_error_and_cancelling_it_is_a_refusal(fake):
    fake.routes[("GET", "/v2/orders:by_client_order_id")] = (404, {"message": "order not found"})
    b = broker(fake)
    with pytest.raises(KeyError):
        b.get_order_status("nope-nope-nope")
    ack = b.cancel_order("nope-nope-nope")
    assert not ack.accepted and ack.reason == "unknown order"


def test_cancel_deletes_by_broker_id_and_a_filled_order_is_refused(fake):
    fake.routes[("GET", "/v2/orders:by_client_order_id")] = (200, order_json())
    fake.routes[("DELETE", "/v2/orders/ord-1")] = (204, None)
    ack = broker(fake).cancel_order("paper-abcd1234-1-aaaaaa")
    assert ack.accepted and ack.broker_order_id == "ord-1"
    fake.routes[("DELETE", "/v2/orders/ord-1")] = (422, {"message": "order is not cancelable"})
    ack = broker(fake).cancel_order("paper-abcd1234-1-aaaaaa")
    assert not ack.accepted and "not cancelable" in ack.reason


def test_open_orders_are_listed_with_their_states(fake):
    fake.routes[("GET", "/v2/orders")] = (200, [order_json(), order_json(id="ord-2", client_order_id="c2", status="partially_filled", filled_qty="1")])
    got = broker(fake).get_open_orders()
    assert [o.state for o in got] == [OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED]
    assert fake.seen("GET", "/v2/orders")[0]["query"]["status"] == "open"


# ---- account --------------------------------------------------------------------------------------------------
def test_positions_report_quantity_and_average_cost_and_a_short_is_visible(fake):
    fake.routes[("GET", "/v2/positions")] = (200, [{"symbol": "PLTR", "qty": "2.5", "avg_entry_price": "40.5"},
                                                   {"symbol": "TSLA", "qty": "-1", "avg_entry_price": "200"}])
    got = {p.symbol: p for p in broker(fake).get_positions()}
    assert got["PLTR"].qty == 2.5 and got["PLTR"].avg_cost == 40.5 and got["TSLA"].qty == -1


def test_cash_is_the_accounts_cash_never_its_buying_power(fake):
    fake.routes[("GET", "/v2/account")] = (200, {"cash": "670.25", "buying_power": "1340.50", "currency": "USD", "status": "ACTIVE"})
    c = broker(fake).get_cash()
    assert c.settled_cash == 670.25 and c.unsettled_cash == 0.0 and c.buying_power == 670.25


@pytest.mark.parametrize("acct", [{"cash": "-5.0"}, {"cash": "abc"}, {"cash": "nan"}, {"currency": "JPY", "cash": "1"}, {}])
def test_an_impossible_cash_answer_fails_closed(fake, acct):
    fake.routes[("GET", "/v2/account")] = (200, acct)
    with pytest.raises(BrokerDisconnectedError):
        broker(fake).get_cash()


@pytest.mark.parametrize("acct,blocked", [
    ({"status": "ACTIVE"}, False), ({"status": "ACTIVE", "trading_blocked": True}, True),
    ({"status": "ACTIVE", "account_blocked": True}, True), ({"status": "ACCOUNT_UPDATED"}, True),
])
def test_a_blocked_or_inactive_account_blocks_trading(fake, acct, blocked):
    fake.routes[("GET", "/v2/account")] = (200, acct)
    st = broker(fake).get_account_status()
    assert st.trading_blocked is blocked and (bool(st.reason) is blocked)


def test_the_account_configuration_must_be_cash_style_and_long_only(fake):
    fake.routes[("GET", "/v2/account")] = (200, {"multiplier": "1"})
    fake.routes[("GET", "/v2/account/configurations")] = (200, {"max_margin_multiplier": "1", "no_shorting": True})
    broker(fake).verify_account_configuration()                      # fine
    fake.routes[("GET", "/v2/account/configurations")] = (200, {"max_margin_multiplier": "2", "no_shorting": False})
    with pytest.raises(AlpacaConfigurationError) as e:
        broker(fake).verify_account_configuration()
    assert "max_margin_multiplier" in str(e.value) and "no_shorting" in str(e.value)
    fake.routes[("GET", "/v2/account/configurations")] = (200, {"max_margin_multiplier": "1", "no_shorting": True})
    fake.routes[("GET", "/v2/account")] = (200, {"multiplier": "4"})
    with pytest.raises(AlpacaConfigurationError):
        broker(fake).verify_account_configuration()
    assert not fake.seen("PATCH", "/v2/account/configurations")        # read-only: it never changes the account


# ---- fills ----------------------------------------------------------------------------------------------------
def activity(i, order_id="ord-1", qty="1", price="40.5", side="buy", ts="2026-11-13T15:00:05.123456789Z"):
    return {"id": f"2026111315::{i}", "order_id": order_id, "symbol": "PLTR", "side": side, "qty": qty,
            "price": price, "transaction_time": ts, "type": "fill"}


def test_the_fills_of_one_order_are_picked_out_of_the_activity_feed(fake):
    fake.routes[("GET", "/v2/orders:by_client_order_id")] = (200, order_json())
    fake.routes[("GET", "/v2/account/activities/FILL")] = (200, [activity(1), activity(2, order_id="other"), activity(3, qty="1.5", price="40.6")])
    fills = broker(fake).get_order_fills("paper-abcd1234-1-aaaaaa")
    assert [(f.broker_fill_id, f.qty, f.price) for f in fills] == [("2026111315::1", 1.0, 40.5), ("2026111315::3", 1.5, 40.6)]
    assert all(f.client_order_id == "paper-abcd1234-1-aaaaaa" and f.side is Action.BUY and f.fees == 0.0 for f in fills)
    assert fills[0].ts == datetime(2026, 11, 13, 15, 0, 5, 123456, tzinfo=timezone.utc)       # nanoseconds are cut to microseconds
    q = fake.seen("GET", "/v2/account/activities/FILL")[0]["query"]
    assert q["after"].startswith("2026-11-13T14:59:00") and q["direction"] == "asc"          # one minute before the order was created


def test_fills_since_are_paged_and_carry_our_client_order_ids(fake):
    pages = [(200, [activity(1), activity(2)]), (200, [activity(3)])]
    fake.routes[("GET", "/v2/account/activities/FILL")] = pages
    fake.routes[("GET", "/v2/orders/ord-1")] = (200, order_json())
    got = broker(fake, page_size=2).get_fills(datetime(2026, 11, 13, tzinfo=timezone.utc))
    assert [f.broker_fill_id for f in got] == ["2026111315::1", "2026111315::2", "2026111315::3"]
    reqs = fake.seen("GET", "/v2/account/activities/FILL")
    assert len(reqs) == 2 and reqs[1]["query"]["page_token"] == "2026111315::2"
    assert len(fake.seen("GET", "/v2/orders/ord-1")) == 1                                     # the order lookup is cached
    assert {f.client_order_id for f in got} == {"paper-abcd1234-1-aaaaaa"}


def test_a_fill_with_an_impossible_number_fails_closed(fake):
    fake.routes[("GET", "/v2/orders:by_client_order_id")] = (200, order_json())
    fake.routes[("GET", "/v2/account/activities/FILL")] = (200, [activity(1, qty="0")])
    with pytest.raises(BrokerDisconnectedError):
        broker(fake).get_order_fills("paper-abcd1234-1-aaaaaa")


# ---- reads are retried a little, then fail closed ------------------------------------------------------------------
def test_a_read_is_retried_on_rate_limit_and_server_errors_then_succeeds(fake):
    fake.routes[("GET", "/v2/positions")] = [(429, {"message": "slow down"}), (503, {"message": "x"}), (200, [])]
    assert broker(fake).get_positions() == [] and len(fake.seen("GET", "/v2/positions")) == 3


def test_a_read_that_keeps_failing_is_a_disconnect(fake):
    fake.routes[("GET", "/v2/positions")] = (503, {"message": "down"})
    with pytest.raises(BrokerDisconnectedError):
        broker(fake).get_positions()
    assert len(fake.seen("GET", "/v2/positions")) == 3
    fake.routes[("GET", "/v2/account")] = (403, {"message": "forbidden"})
    with pytest.raises(BrokerDisconnectedError):
        broker(fake).get_cash()
