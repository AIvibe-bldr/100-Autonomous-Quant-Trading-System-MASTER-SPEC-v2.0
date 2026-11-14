"""Alpaca broker adapter — PAPER endpoint only (MASTER SPEC §100, §73; docs/HANDOFF_next_session.md §3b).

The one place that talks to Alpaca's trading API. Choices that matter for a safety path:

- The environment is DERIVED FROM THE HOST this adapter is built for, never from a flag: only the paper
  host (`paper-api.alpaca.markets`) is accepted — the live host and any unknown host are refused at
  construction, so no code path in this version can send a real order. (Plain http is accepted for the
  loopback address only, so the adapter can be tested against a local server.)
- Keys come from the ENVIRONMENT only (`QUANT_ALPACA_TRADING_KEY_ID` / `QUANT_ALPACA_TRADING_SECRET`,
  separate from the market-data keys) and never appear in an exception, a log line or a URL.
- A submit is NEVER retried: when its outcome is unknown the engine moves the order to UNKNOWN and
  reconciles (§45-46). Reads are retried a couple of times on rate-limit / 5xx answers, then fail closed.
- Anything unexpected (an unknown order status, an unreadable number, a negative cash balance) fails
  closed: an UNKNOWN order state or a BrokerDisconnectedError, both of which halt rather than guess.
- Prices are sent on the exchange tick (a cent, or 1/100 cent below $1) rounded so the order is never
  worse than approved: sells round up, buys round down.

Known limits (see docs/paper_live_gate.md): fees are reported as 0 (Alpaca equities are commission-free;
regulatory fees surface as cash differences that reconciliation catches); unsettled cash is reported as 0
(T+1 is not modelled); an order cancelled after a partial fill is reported CANCELLED and its fills are not
routed — reconciliation sees the position difference and halts new entries.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any, Callable, Iterator, Optional

from packages.broker_adapters.base import (
    BrokerAdapter,
    BrokerDisconnectedError,
    BrokerTimeoutError,
    DuplicateClientOrderIdError,
)
from packages.common.environment import Environment
from packages.schemas.core import (
    Action,
    BrokerAccountStatus,
    BrokerAck,
    BrokerCashBalance,
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
    OrderState,
    OrderType,
)

PAPER_HOST = "paper-api.alpaca.markets"
_LOOPBACK = ("127.0.0.1", "localhost")
_USER_AGENT = "quant-broker/1.0"          # a generic name: no contact details (Discord/Cloudflare reject urllib's default)
_MAX_BODY = 1_000_000
_RETRY_STATUSES = (429, 502, 503, 504)
_MAX_PAGES = 50

KEY_ENV, SECRET_ENV = "QUANT_ALPACA_TRADING_KEY_ID", "QUANT_ALPACA_TRADING_SECRET"

# Alpaca order status -> our OrderState. Anything not listed (stopped, suspended, replaced, done_for_day, ...)
# is UNKNOWN on purpose: an order we cannot classify must trigger reconciliation, not be guessed.
_STATE = {
    "pending_new": OrderState.SUBMITTED,
    "new": OrderState.ACKNOWLEDGED, "accepted": OrderState.ACKNOWLEDGED,
    "accepted_for_bidding": OrderState.ACKNOWLEDGED, "held": OrderState.ACKNOWLEDGED,
    "calculated": OrderState.ACKNOWLEDGED,
    "partially_filled": OrderState.PARTIALLY_FILLED, "filled": OrderState.FILLED,
    "pending_cancel": OrderState.CANCEL_REQUESTED, "canceled": OrderState.CANCELLED,
    "expired": OrderState.EXPIRED, "rejected": OrderState.REJECTED,
}


class AlpacaConfigurationError(RuntimeError):
    """The account is not set up the way this system requires (cash-style, long-only)."""


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        return None


def _number(data: dict, key: str, *, minimum: Optional[float] = None) -> float:
    try:
        value = float(data[key])
    except (KeyError, TypeError, ValueError):
        raise BrokerDisconnectedError(f"unexpected broker response: '{key}' is missing or not a number") from None
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise BrokerDisconnectedError(f"unexpected broker response: '{key}' has an impossible value")
    return value


def _timestamp(text: Any) -> datetime:
    """Alpaca timestamps carry up to nanoseconds ('...123456789Z'); fromisoformat takes microseconds."""
    try:
        s = str(text).replace("Z", "+00:00")
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        ts = datetime.fromisoformat(s)
    except ValueError:
        raise BrokerDisconnectedError("unexpected broker response: unreadable timestamp") from None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _qty_text(qty: float) -> str:
    d = Decimal(repr(qty)).quantize(Decimal("1E-9"), rounding=ROUND_DOWN)
    return format(d, "f").rstrip("0").rstrip(".") if d > 0 else ""


def _price_text(price: float, side: Action) -> str:
    """On the tick, never worse than approved: a SELL (limit or protective stop) rounds up — a stop then triggers
    marginally earlier — and a BUY rounds down."""
    d = Decimal(repr(price))
    tick = Decimal("0.0001") if d < 1 else Decimal("0.01")
    d = d.quantize(tick, rounding=ROUND_CEILING if side is Action.SELL else ROUND_FLOOR)
    return format(d, "f") if d > 0 else ""


class AlpacaBroker(BrokerAdapter):
    def __init__(self, key_id: str, secret_key: str, *, base_url: str = f"https://{PAPER_HOST}",
                 timeout: float = 10.0, page_size: int = 100,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not key_id or not secret_key:
            raise ValueError("Alpaca trading keys are required")
        parts = urllib.parse.urlsplit(base_url)
        ok = (parts.scheme == "https" and parts.hostname == PAPER_HOST) or \
             (parts.scheme == "http" and parts.hostname in _LOOPBACK)
        if not ok:
            raise ValueError(f"this adapter only connects to the Alpaca PAPER endpoint ({PAPER_HOST}); "
                             f"refusing host {parts.hostname!r}")
        self._base = base_url.rstrip("/")
        self._timeout, self._page_size, self._sleep = timeout, page_size, sleep
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key,
                         "Content-Type": "application/json", "Accept": "application/json",
                         "User-Agent": _USER_AGENT}
        self._client_ids: dict[str, str] = {}          # broker order id -> our client_order_id

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None, **kw: Any) -> "AlpacaBroker":
        env = dict(os.environ if env is None else env)
        key, secret = env.get(KEY_ENV, "").strip(), env.get(SECRET_ENV, "").strip()
        if not key or not secret:
            raise ValueError(f"set {KEY_ENV} and {SECRET_ENV} (the paper trading keys)")
        return cls(key, secret, **kw)

    @property
    def environment(self) -> Environment:
        return Environment.PAPER

    # -- transport ----------------------------------------------------------------
    def _send(self, method: str, path: str, *, query: Optional[dict] = None, body: Optional[dict] = None,
              timed_out: type = BrokerDisconnectedError) -> tuple[int, Any, float]:
        url = self._base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, _parse_json(resp.read(_MAX_BODY)), 0.0
        except urllib.error.HTTPError as e:
            try:
                retry_after = float(e.headers.get("Retry-After", "1"))
            except (TypeError, ValueError):
                retry_after = 1.0
            return e.code, _parse_json(e.read(_MAX_BODY)), min(max(retry_after, 0.0), 5.0)
        except (socket.timeout, TimeoutError):
            raise timed_out("the broker did not answer in time") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise timed_out("the broker did not answer in time") from None
            raise BrokerDisconnectedError(f"cannot reach the broker ({type(e.reason).__name__})") from None
        except OSError as e:
            raise BrokerDisconnectedError(f"cannot reach the broker ({type(e).__name__})") from None

    @staticmethod
    def _message(data: Any) -> str:
        return str(data.get("message", ""))[:200] if isinstance(data, dict) else ""

    def _get(self, path: str, query: Optional[dict] = None, *, allow_404: bool = False) -> Any:
        """A read: retried on rate-limit / 5xx answers, then fails closed. Returns None for an allowed 404."""
        for attempt in range(3):
            status, data, wait = self._send("GET", path, query=query)
            if status in _RETRY_STATUSES and attempt < 2:
                self._sleep(wait)
                continue
            if 200 <= status < 300:
                return data
            if status == 404 and allow_404:
                return None
            if status in (401, 403):
                raise BrokerDisconnectedError(f"the broker refused the credentials or the request (HTTP {status})")
            raise BrokerDisconnectedError(f"unexpected answer from the broker (HTTP {status}) {self._message(data)}".strip())
        raise BrokerDisconnectedError("the broker is busy or unavailable")      # pragma: no cover (loop always returns/raises)

    # -- orders -------------------------------------------------------------------
    def submit_order(self, order: BrokerOrderRequest) -> BrokerAck:
        cid = order.client_order_id
        qty = _qty_text(order.qty)
        if not qty:
            return BrokerAck(client_order_id=cid, broker_order_id="", accepted=False, reason="quantity too small to send")
        body: dict[str, Any] = {"symbol": order.symbol, "qty": qty, "side": order.side.value.lower(),
                                "type": order.order_type.value.lower(),
                                "time_in_force": order.time_in_force.value.lower(), "client_order_id": cid}
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            body["limit_price"] = _price_text(order.limit_price, order.side)
        if order.order_type is OrderType.STOP and order.stop_price is not None:
            body["stop_price"] = _price_text(order.stop_price, order.side)
        if "" in (body.get("limit_price"), body.get("stop_price")):
            return BrokerAck(client_order_id=cid, broker_order_id="", accepted=False, reason="price too small to send")
        # never retried: when the outcome is unknown the engine reconciles (§45-46)
        status, data, _ = self._send("POST", "/v2/orders", body=body, timed_out=BrokerTimeoutError)
        message = self._message(data)
        if status == 200 and isinstance(data, dict) and data.get("id"):
            return BrokerAck(client_order_id=cid, broker_order_id=str(data["id"]), accepted=True)
        if status == 422 and "client_order_id" in message.lower() and "unique" in message.lower():
            raise DuplicateClientOrderIdError(cid)
        if status in (401, 429) or status >= 500:
            # 401: cannot act at all; 429/5xx: the request may or may not have been processed
            if status == 401:
                raise BrokerDisconnectedError("the broker refused the credentials (HTTP 401)")
            raise BrokerTimeoutError(f"the broker answered HTTP {status}; the order's fate is unknown")
        return BrokerAck(client_order_id=cid, broker_order_id="", accepted=False,
                         reason=f"HTTP {status}: {message}".strip())

    def _order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        data = self._get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id}, allow_404=True)
        if data is None:
            return None
        if not isinstance(data, dict) or "id" not in data:
            raise BrokerDisconnectedError("unexpected broker response: not an order")
        self._client_ids[str(data["id"])] = client_order_id
        return data

    @staticmethod
    def _status_of(order: dict) -> BrokerOrderStatus:
        filled = _number(order, "filled_qty", minimum=0.0)
        qty = _number(order, "qty", minimum=0.0)
        return BrokerOrderStatus(client_order_id=str(order.get("client_order_id", "")), broker_order_id=str(order["id"]),
                                 state=_STATE.get(str(order.get("status", "")).lower(), OrderState.UNKNOWN),
                                 filled_qty=filled, remaining_qty=max(0.0, qty - filled))

    def get_order_status(self, client_order_id: str) -> BrokerOrderStatus:
        order = self._order_by_client_id(client_order_id)
        if order is None:
            raise KeyError(client_order_id)
        return self._status_of(order)

    def cancel_order(self, client_order_id: str) -> BrokerAck:
        order = self._order_by_client_id(client_order_id)
        if order is None:
            return BrokerAck(client_order_id=client_order_id, broker_order_id="", accepted=False, reason="unknown order")
        oid = str(order["id"])
        status, data, _ = self._send("DELETE", f"/v2/orders/{urllib.parse.quote(oid, safe='')}")
        if status in (200, 204):
            return BrokerAck(client_order_id=client_order_id, broker_order_id=oid, accepted=True)
        if status in (401, 403):
            raise BrokerDisconnectedError(f"the broker refused the credentials or the request (HTTP {status})")
        if status == 429 or status >= 500:
            raise BrokerDisconnectedError(f"the broker answered HTTP {status}; the cancel's outcome is unknown")
        return BrokerAck(client_order_id=client_order_id, broker_order_id=oid, accepted=False,
                         reason=f"not cancellable: {self._message(data)}".strip())

    def get_open_orders(self) -> list[BrokerOrderStatus]:
        data = self._get("/v2/orders", {"status": "open", "limit": "500", "direction": "desc"})
        if not isinstance(data, list):
            raise BrokerDisconnectedError("unexpected broker response: not a list of orders")
        return [self._status_of(o) for o in data]

    # -- account ------------------------------------------------------------------
    def get_positions(self) -> list[BrokerPosition]:
        data = self._get("/v2/positions")
        if not isinstance(data, list):
            raise BrokerDisconnectedError("unexpected broker response: not a list of positions")
        return [BrokerPosition(symbol=str(p["symbol"]), qty=_number(p, "qty"),
                               avg_cost=_number(p, "avg_entry_price", minimum=0.0)) for p in data]

    def _account(self) -> dict:
        data = self._get("/v2/account")
        if not isinstance(data, dict):
            raise BrokerDisconnectedError("unexpected broker response: not an account")
        return data

    def get_cash(self) -> BrokerCashBalance:
        acct = self._account()
        if str(acct.get("currency", "USD")).upper() != "USD":
            raise BrokerDisconnectedError("unexpected broker response: the account is not in USD")
        # `cash`, never `buying_power`: this is a cash-style system (§14, ISSUE-2). A negative balance means
        # margin debt — not something to clamp to zero.
        return BrokerCashBalance(settled_cash=_number(acct, "cash", minimum=0.0), unsettled_cash=0.0, currency="USD")

    def get_account_status(self) -> BrokerAccountStatus:
        acct = self._account()
        reasons = [name for name in ("trading_blocked", "account_blocked") if acct.get(name) is True]
        if str(acct.get("status", "ACTIVE")).upper() != "ACTIVE":
            reasons.append(f"status={acct.get('status')}")
        return BrokerAccountStatus(trading_blocked=bool(reasons), reason=", ".join(reasons))

    def verify_account_configuration(self) -> None:
        """Refuse an account that is not cash-style and long-only (Alpaca enforces these settings itself).
        Read-only: the operator changes the settings once (PATCH /v2/account/configurations); this never does."""
        cfg = self._get("/v2/account/configurations")
        acct = self._account()
        if not isinstance(cfg, dict):
            raise BrokerDisconnectedError("unexpected broker response: not an account configuration")
        problems = []
        if str(cfg.get("max_margin_multiplier")) != "1":
            problems.append(f"max_margin_multiplier is {cfg.get('max_margin_multiplier')!r}, must be '1'")
        if cfg.get("no_shorting") is not True:
            problems.append(f"no_shorting is {cfg.get('no_shorting')!r}, must be true")
        if str(acct.get("multiplier", "1")) != "1":
            problems.append(f"the account's margin multiplier is {acct.get('multiplier')!r}, must be '1'")
        if problems:
            raise AlpacaConfigurationError("; ".join(problems))

    # -- fills --------------------------------------------------------------------
    def _client_id_of(self, broker_order_id: str) -> str:
        if broker_order_id not in self._client_ids:
            order = self._get(f"/v2/orders/{urllib.parse.quote(broker_order_id, safe='')}", allow_404=True)
            self._client_ids[broker_order_id] = str(order.get("client_order_id", broker_order_id)) \
                if isinstance(order, dict) else broker_order_id
        return self._client_ids[broker_order_id]

    def _fill_activities(self, after: datetime) -> Iterator[dict]:
        token: Optional[str] = None
        for _ in range(_MAX_PAGES):
            query = {"after": after.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                     "direction": "asc", "page_size": str(self._page_size)}
            if token:
                query["page_token"] = token
            page = self._get("/v2/account/activities/FILL", query)
            if not isinstance(page, list):
                raise BrokerDisconnectedError("unexpected broker response: not a list of fills")
            yield from page
            if len(page) < self._page_size:
                return
            token = str(page[-1]["id"])
        raise BrokerDisconnectedError("too many fills to read: pass a narrower 'since'")

    def _fill_of(self, a: dict, client_order_id: Optional[str] = None) -> BrokerFill:
        return BrokerFill(broker_fill_id=str(a["id"]),
                          client_order_id=client_order_id or self._client_id_of(str(a["order_id"])),
                          symbol=str(a["symbol"]), side=Action(str(a["side"]).upper()),
                          qty=_number(a, "qty", minimum=1e-12), price=_number(a, "price", minimum=1e-12),
                          fees=0.0, ts=_timestamp(a["transaction_time"]))

    def get_fills(self, since: datetime) -> list[BrokerFill]:
        return [self._fill_of(a) for a in self._fill_activities(since)]

    def get_order_fills(self, client_order_id: str) -> list[BrokerFill]:
        order = self._order_by_client_id(client_order_id)
        if order is None:
            raise KeyError(client_order_id)
        start = _timestamp(order.get("created_at") or order.get("submitted_at")) - timedelta(minutes=1)
        return [self._fill_of(a, client_order_id) for a in self._fill_activities(start)
                if str(a.get("order_id")) == str(order["id"])]
