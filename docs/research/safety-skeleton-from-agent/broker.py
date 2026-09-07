"""Broker boundary: a narrow Protocol, an Alpaca adapter that translates errors, and a FakeBroker with fault
injection for tests. Nothing outside this module imports alpaca-py."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Protocol

PAPER_URL = "https://paper-api.alpaca.markets"
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped"})


class BrokerError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status, self.code = status, code


class AuthError(BrokerError):
    pass


class TransientError(BrokerError):
    """429 / 5xx / timeouts / connection errors. The request MAY have been applied."""


class DuplicateClientOrderId(BrokerError):
    pass


class OrderRejected(BrokerError):
    """403 / 422 (other than duplicate id). Never retried."""


class NotFound(BrokerError):
    pass


@dataclass(frozen=True)
class AccountSnap:
    id: str
    account_number: str
    status: str
    equity: float
    last_equity: float
    cash: float
    buying_power: float
    trading_blocked: bool = False
    account_blocked: bool = False
    pattern_day_trader: bool = False


@dataclass(frozen=True)
class PositionSnap:
    symbol: str
    qty: float
    qty_available: float
    market_value: float
    current_price: float


@dataclass(frozen=True)
class OrderSnap:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    filled_avg_price: Optional[float]
    status: str
    submitted_at: datetime


@dataclass(frozen=True)
class OrderReq:
    symbol: str
    side: str
    qty: int
    client_order_id: str
    order_type: str = "market"
    tif: str = "day"


@dataclass(frozen=True)
class ClockSnap:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class SessionDay:
    day: date
    open: datetime
    close: datetime


class Broker(Protocol):
    base_url: str

    def get_account(self) -> AccountSnap: ...
    def get_positions(self) -> list[PositionSnap]: ...
    def get_orders(self, status: str = "all", after: Optional[datetime] = None) -> list[OrderSnap]: ...
    def get_order_by_client_id(self, coid: str) -> Optional[OrderSnap]: ...
    def submit(self, req: OrderReq) -> OrderSnap: ...
    def cancel(self, order_id: str) -> None: ...
    def get_clock(self) -> ClockSnap: ...
    def get_calendar(self, start: date, end: date) -> list[SessionDay]: ...


def _f(x: object, default: float = 0.0) -> float:
    return float(x) if x not in (None, "") else default


class AlpacaBroker:
    """Thin adapter over alpaca-py TradingClient. Note: the SDK itself retries 429/504 (3x, 3s) on every method,
    including POST /orders, so client_order_id is mandatory on every submit."""

    def __init__(self, key_id: str, secret_key: str, paper: bool) -> None:
        from alpaca.trading.client import TradingClient

        self._c = TradingClient(key_id, secret_key, paper=paper)
        raw = self._c._base_url
        self.base_url = str(getattr(raw, "value", raw))

    @staticmethod
    def _translate(err: Exception) -> BrokerError:
        from alpaca.common.exceptions import APIError

        if not isinstance(err, APIError):
            return TransientError(f"{type(err).__name__}: {err}")
        status = err.status_code
        try:
            code, msg = err.code, err.message
        except Exception:  # body not JSON
            code, msg = None, str(err)
        if status == 401:
            return AuthError("alpaca 401 unauthorized: keys invalid or rotated", status, code)
        if status == 404:
            return NotFound(msg, status, code)
        if status == 422 and "unique" in (msg or "").lower():
            return DuplicateClientOrderId(msg, status, code)
        if status in (403, 422):
            return OrderRejected(msg, status, code)
        if status == 429 or (status is not None and status >= 500):
            return TransientError(msg, status, code)
        return BrokerError(msg, status, code)

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - translated at the boundary
            raise self._translate(e) from None

    def get_account(self) -> AccountSnap:
        a = self._call(self._c.get_account)
        return AccountSnap(str(a.id), a.account_number, str(getattr(a.status, "value", a.status)), _f(a.equity),
                           _f(a.last_equity), _f(a.cash), _f(a.buying_power), bool(a.trading_blocked),
                           bool(a.account_blocked), bool(a.pattern_day_trader))

    def get_positions(self) -> list[PositionSnap]:
        return [PositionSnap(p.symbol, _f(p.qty), _f(p.qty_available, _f(p.qty)), _f(p.market_value), _f(p.current_price))
                for p in self._call(self._c.get_all_positions)]

    @staticmethod
    def _order(o) -> OrderSnap:
        return OrderSnap(str(o.id), o.client_order_id, o.symbol, str(getattr(o.side, "value", o.side)), _f(o.qty),
                         _f(o.filled_qty), _f(o.filled_avg_price) if o.filled_avg_price else None,
                         str(getattr(o.status, "value", o.status)), o.submitted_at)

    def get_orders(self, status: str = "all", after: Optional[datetime] = None) -> list[OrderSnap]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus(status), after=after, limit=500)
        return [self._order(o) for o in self._call(self._c.get_orders, req)]

    def get_order_by_client_id(self, coid: str) -> Optional[OrderSnap]:
        try:
            return self._order(self._call(self._c.get_order_by_client_id, coid))
        except NotFound:
            return None

    def submit(self, req: OrderReq) -> OrderSnap:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if req.order_type != "market" or req.tif != "day":
            raise OrderRejected("skeleton supports market/day only")
        mo = MarketOrderRequest(symbol=req.symbol, qty=req.qty, side=OrderSide(req.side),
                                time_in_force=TimeInForce.DAY, client_order_id=req.client_order_id)
        return self._order(self._call(self._c.submit_order, mo))

    def cancel(self, order_id: str) -> None:
        self._call(self._c.cancel_order_by_id, order_id)

    def get_clock(self) -> ClockSnap:
        c = self._call(self._c.get_clock)
        return ClockSnap(c.timestamp, c.is_open, c.next_open, c.next_close)

    def get_calendar(self, start: date, end: date) -> list[SessionDay]:
        from alpaca.trading.requests import GetCalendarRequest

        days = self._call(self._c.get_calendar, GetCalendarRequest(start=start, end=end))
        return [SessionDay(d.date, d.open, d.close) for d in days]


class FakeBroker:
    """In-memory broker for unit/replay tests and DRY-RUN. Fault injection via fail_submit_once:
    '504-after-accept' (ambiguous: order stored, then error), '504-before-accept', '429', or None."""

    def __init__(self, *, cash: float = 100_000.0, prices: Optional[dict[str, float]] = None,
                 positions: Optional[dict[str, float]] = None, account_number: str = "PA000TEST",
                 account_id: str = "fake-acct-1", base_url: str = PAPER_URL, last_equity: Optional[float] = None,
                 now: Optional[datetime] = None) -> None:
        self.base_url = base_url
        self.cash = cash
        self.prices = dict(prices or {})
        self._pos: dict[str, float] = dict(positions or {})
        self._orders: dict[str, OrderSnap] = {}
        self.account_number, self.account_id = account_number, account_id
        self.now = now or datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
        self._last_equity = last_equity if last_equity is not None else self.equity()
        self.calls: list[tuple] = []
        self.fail_submit_once: Optional[str] = None
        self.partial_fill_ratio: Optional[float] = None
        self.reject_symbols: set[str] = set()
        self.market_open = True
        self.holidays: set[date] = set()

    # --- helpers -------------------------------------------------------------------------------
    def equity(self) -> float:
        return self.cash + sum(q * self.prices.get(s, 0.0) for s, q in self._pos.items())

    def fill_open_orders(self) -> None:
        for oid, o in list(self._orders.items()):
            if o.status in TERMINAL:
                continue
            remaining = o.qty - o.filled_qty
            fill = remaining if self.partial_fill_ratio is None else max(1.0, round(remaining * self.partial_fill_ratio))
            px = self.prices[o.symbol]
            sign = 1 if o.side == "buy" else -1
            self._pos[o.symbol] = self._pos.get(o.symbol, 0.0) + sign * fill
            self.cash -= sign * fill * px
            if self._pos[o.symbol] == 0:
                del self._pos[o.symbol]
            done = o.filled_qty + fill >= o.qty
            self._orders[oid] = replace(o, filled_qty=o.filled_qty + fill, filled_avg_price=px,
                                        status="filled" if done else "partially_filled")

    # --- Broker protocol -----------------------------------------------------------------------
    def get_account(self) -> AccountSnap:
        self.calls.append(("get_account",))
        return AccountSnap(self.account_id, self.account_number, "ACTIVE", self.equity(), self._last_equity,
                           self.cash, self.cash)

    def get_positions(self) -> list[PositionSnap]:
        self.calls.append(("get_positions",))
        reserved = {s: sum(o.qty - o.filled_qty for o in self._orders.values()
                           if o.symbol == s and o.side == "sell" and o.status not in TERMINAL) for s in self._pos}
        return [PositionSnap(s, q, q - reserved[s], q * self.prices.get(s, 0.0), self.prices.get(s, 0.0))
                for s, q in sorted(self._pos.items())]

    def get_orders(self, status: str = "all", after: Optional[datetime] = None) -> list[OrderSnap]:
        self.calls.append(("get_orders", status))
        out = list(self._orders.values())
        if status == "open":
            out = [o for o in out if o.status not in TERMINAL]
        elif status == "closed":
            out = [o for o in out if o.status in TERMINAL]
        if after is not None:
            out = [o for o in out if o.submitted_at >= after]
        return out

    def get_order_by_client_id(self, coid: str) -> Optional[OrderSnap]:
        self.calls.append(("get_order_by_client_id", coid))
        return next((o for o in self._orders.values() if o.client_order_id == coid), None)

    def submit(self, req: OrderReq) -> OrderSnap:
        self.calls.append(("submit", req.client_order_id))
        if self.get_order_by_client_id(req.client_order_id) is not None:
            raise DuplicateClientOrderId("client_order_id must be unique", 422, 40010001)
        mode, self.fail_submit_once = self.fail_submit_once, None
        if mode == "504-before-accept":
            raise TransientError("gateway timeout", 504)
        if mode == "429":
            raise TransientError("too many requests", 429)
        if req.symbol in self.reject_symbols:
            raise OrderRejected("insufficient buying power", 403, 40310000)
        snap = OrderSnap(str(uuid.uuid4()), req.client_order_id, req.symbol, req.side, float(req.qty), 0.0, None,
                         "accepted", self.now)
        self._orders[snap.id] = snap
        if mode == "504-after-accept":
            raise TransientError("gateway timeout (order was accepted server-side)", 504)
        return snap

    def cancel(self, order_id: str) -> None:
        self.calls.append(("cancel", order_id))
        o = self._orders[order_id]
        if o.status not in TERMINAL:
            self._orders[order_id] = replace(o, status="canceled")

    def get_clock(self) -> ClockSnap:
        return ClockSnap(self.now, self.market_open, self.now + timedelta(days=1), self.now + timedelta(hours=2))

    def get_calendar(self, start: date, end: date) -> list[SessionDay]:
        out, d = [], start
        while d <= end:
            if d.weekday() < 5 and d not in self.holidays:
                out.append(SessionDay(d, datetime.combine(d, time(9, 30)), datetime.combine(d, time(16, 0))))
            d += timedelta(days=1)
        return out
