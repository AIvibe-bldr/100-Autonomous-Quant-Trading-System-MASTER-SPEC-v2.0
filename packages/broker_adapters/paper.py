"""Paper Broker (MASTER SPEC §25, §46, §73).

Deterministic execution simulation: fills cross the spread, pay slippage and
fees, and can partially fill — never a naive close-price fill (§25).

Fault injection hooks (`fault`) let chaos tests simulate timeouts,
disconnects and duplicate callbacks (§70).
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from packages.broker_adapters.base import (
    BrokerAdapter,
    BrokerDisconnectedError,
    BrokerTimeoutError,
    DuplicateClientOrderIdError,
)
from packages.common.clock import Clock, ensure_utc
from packages.schemas.core import (
    Action,
    BrokerAck,
    BrokerCashBalance,
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
    OrderState,
    OrderType,
    Quote,
)


class Fault(str, enum.Enum):
    NONE = "NONE"
    TIMEOUT = "TIMEOUT"            # submit raises; order state unknown
    DISCONNECT = "DISCONNECT"      # all calls raise
    PARTIAL_FILL = "PARTIAL_FILL"  # only half the qty fills


@dataclass
class _PaperOrder:
    request: BrokerOrderRequest
    broker_order_id: str
    state: OrderState
    filled_qty: float = 0.0
    fills: list[BrokerFill] = field(default_factory=list)


class PaperBroker(BrokerAdapter):
    """In-memory broker with realistic-enough microstructure for V1 paper runs."""

    def __init__(self, quote_fn: Callable[[str], Quote], clock: Clock,
                 initial_cash: float, fee_bps: float = 1.0, slippage_bps: float = 2.0) -> None:
        self._quote_fn = quote_fn
        self._clock = clock
        self._settled_cash = initial_cash
        self._unsettled_cash = 0.0
        self._fee_bps = fee_bps
        self._slippage_bps = slippage_bps
        self._orders: dict[str, _PaperOrder] = {}  # keyed by client_order_id
        self._positions: dict[str, BrokerPosition] = {}
        self._all_fills: list[BrokerFill] = []
        self.fault: Fault = Fault.NONE

    # -- fault helpers ------------------------------------------------------
    def _check_disconnect(self) -> None:
        if self.fault is Fault.DISCONNECT:
            raise BrokerDisconnectedError("paper broker: simulated disconnect")

    # -- BrokerAdapter ------------------------------------------------------
    def submit_order(self, order: BrokerOrderRequest) -> BrokerAck:
        self._check_disconnect()
        if order.client_order_id in self._orders:
            # §46: duplicates are structurally impossible — reject loudly
            raise DuplicateClientOrderIdError(order.client_order_id)

        po = _PaperOrder(request=order, broker_order_id=str(uuid.uuid4()),
                         state=OrderState.SUBMITTED)
        self._orders[order.client_order_id] = po

        if self.fault is Fault.TIMEOUT:
            # Order was accepted broker-side but the ack never reaches us.
            po.state = OrderState.ACKNOWLEDGED
            self._execute(po)
            raise BrokerTimeoutError(order.client_order_id)

        po.state = OrderState.ACKNOWLEDGED
        self._execute(po)
        return BrokerAck(client_order_id=order.client_order_id,
                         broker_order_id=po.broker_order_id, accepted=True)

    def _fill_price(self, req: BrokerOrderRequest, quote: Quote) -> Optional[float]:
        slip = quote.mid * self._slippage_bps / 10_000
        if req.order_type is OrderType.MARKET:
            return (quote.ask + slip) if req.side is Action.BUY else max(0.01, quote.bid - slip)
        if req.order_type is OrderType.LIMIT:
            assert req.limit_price is not None
            if req.side is Action.BUY and quote.ask <= req.limit_price:
                return quote.ask
            if req.side is Action.SELL and quote.bid >= req.limit_price:
                return quote.bid
            return None  # resting
        if req.order_type is OrderType.STOP:
            assert req.stop_price is not None
            # protective sell-stop triggers when bid <= stop
            if req.side is Action.SELL and quote.bid <= req.stop_price:
                return max(0.01, quote.bid - slip)
            return None
        return None

    def _execute(self, po: _PaperOrder) -> None:
        req = po.request
        quote = self._quote_fn(req.symbol)
        px = self._fill_price(req, quote)
        if px is None:
            return  # resting order stays ACKNOWLEDGED
        # fill only what is still outstanding, so re-polling a resting or
        # partially filled order can never over-fill it
        remaining = req.qty - po.filled_qty
        if remaining <= 1e-9:
            return
        qty = remaining
        if self.fault is Fault.PARTIAL_FILL:
            qty = remaining / 2

        cost = qty * px
        fees = cost * self._fee_bps / 10_000

        def _cannot_fill() -> None:
            # an order that already produced fills cannot be retro-rejected;
            # its remainder simply stays resting until cancelled or expired
            if po.filled_qty <= 1e-9:
                po.state = OrderState.REJECTED

        if req.side is Action.BUY:
            if cost + fees > self._settled_cash + 1e-9:
                _cannot_fill()
                return
            self._settled_cash -= cost + fees
            pos = self._positions.get(req.symbol)
            new_qty = (pos.qty if pos else 0.0) + qty
            new_cost = (((pos.qty * pos.avg_cost) if pos else 0.0) + cost) / new_qty
            self._positions[req.symbol] = BrokerPosition(symbol=req.symbol, qty=new_qty,
                                                         avg_cost=round(new_cost, 6))
        else:
            pos = self._positions.get(req.symbol)
            if pos is None or pos.qty + 1e-9 < qty:
                _cannot_fill()  # no shorting in a cash account (§2)
                return
            remaining = pos.qty - qty
            if remaining <= 1e-9:
                del self._positions[req.symbol]
            else:
                self._positions[req.symbol] = BrokerPosition(symbol=req.symbol, qty=remaining,
                                                             avg_cost=pos.avg_cost)
            # sale proceeds are unsettled until T+1 (§14)
            self._unsettled_cash += cost - fees

        fill = BrokerFill(broker_fill_id=str(uuid.uuid4()), client_order_id=req.client_order_id,
                          symbol=req.symbol, side=req.side, qty=qty, price=round(px, 6),
                          fees=round(fees, 6), ts=self._clock.now())
        po.fills.append(fill)
        self._all_fills.append(fill)
        po.filled_qty += qty
        po.state = (OrderState.FILLED if po.filled_qty >= req.qty - 1e-9
                    else OrderState.PARTIALLY_FILLED)

    def settle(self) -> None:
        """T+1 settlement tick: move unsettled proceeds to settled cash."""
        self._settled_cash += self._unsettled_cash
        self._unsettled_cash = 0.0

    def poll_resting_orders(self) -> list[BrokerFill]:
        """Re-evaluate resting orders against current quotes.

        A real broker watches resting stop/limit orders continuously; without
        this, a protective stop submitted today could never trigger tomorrow.
        Returns fills produced by this evaluation.
        """
        self._check_disconnect()
        before = len(self._all_fills)
        for po in list(self._orders.values()):
            if po.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
                self._execute(po)
        return self._all_fills[before:]

    def cancel_order(self, client_order_id: str) -> BrokerAck:
        self._check_disconnect()
        po = self._orders.get(client_order_id)
        if po is None:
            return BrokerAck(client_order_id=client_order_id, broker_order_id="",
                             accepted=False, reason="unknown order")
        if po.state in (OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED):
            po.state = OrderState.CANCELLED
            return BrokerAck(client_order_id=client_order_id,
                             broker_order_id=po.broker_order_id, accepted=True)
        return BrokerAck(client_order_id=client_order_id, broker_order_id=po.broker_order_id,
                         accepted=False, reason=f"not cancellable from {po.state}")

    def get_order_status(self, client_order_id: str) -> BrokerOrderStatus:
        self._check_disconnect()
        po = self._orders[client_order_id]
        return BrokerOrderStatus(client_order_id=client_order_id,
                                 broker_order_id=po.broker_order_id, state=po.state,
                                 filled_qty=po.filled_qty,
                                 remaining_qty=max(0.0, po.request.qty - po.filled_qty))

    def get_positions(self) -> list[BrokerPosition]:
        self._check_disconnect()
        return list(self._positions.values())

    def get_cash(self) -> BrokerCashBalance:
        self._check_disconnect()
        return BrokerCashBalance(settled_cash=round(self._settled_cash, 6),
                                 unsettled_cash=round(self._unsettled_cash, 6))

    def get_open_orders(self) -> list[BrokerOrderStatus]:
        self._check_disconnect()
        return [self.get_order_status(cid) for cid, po in self._orders.items()
                if po.state in (OrderState.SUBMITTED, OrderState.ACKNOWLEDGED,
                                OrderState.PARTIALLY_FILLED)]

    def get_fills(self, since: datetime) -> list[BrokerFill]:
        self._check_disconnect()
        since = ensure_utc(since)
        return [f for f in self._all_fills if f.ts >= since]
