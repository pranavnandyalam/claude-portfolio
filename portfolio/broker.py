"""Broker abstraction: Alpaca (paper by default, hard-gated live) + an in-memory FakeBroker.

Secrets policy
- Keys are loaded from the process env, then ./ .env, then ~/.claude/state/alpaca/.env.
- Keys are never logged, printed, or returned by any public function.

Paper/live gate
- AlpacaBroker() is PAPER unless live=True AND env PORTFOLIO_ALLOW_LIVE=1 AND the file
  state/LIVE_CONFIRMED exists containing the exact line 'I UNDERSTAND THIS TRADES REAL MONEY'.
- After connecting, the account's paper-ness is re-verified from the API base URL.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIVE_CONFIRM_FILE = PROJECT_ROOT / "state" / "LIVE_CONFIRMED"
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS TRADES REAL MONEY"
LIVE_ENV_FLAG = "PORTFOLIO_ALLOW_LIVE"
KEY_FILES = (PROJECT_ROOT / ".env", Path.home() / ".claude" / "state" / "alpaca" / ".env")
MIN_NOTIONAL_USD = 1.0
QTY_DECIMALS = 6


class BrokerError(RuntimeError):
    """Any broker-side failure (auth, rejected order, network)."""


class AuthError(BrokerError):
    """Keys missing or rejected (HTTP 401/403)."""


class LiveTradingBlocked(BrokerError):
    """Attempted to construct a live broker without the explicit gate."""


# --------------------------------------------------------------------------- models

@dataclass(frozen=True)
class Account:
    equity: float
    cash: float
    buying_power: float
    last_equity: float
    status: str
    is_paper: bool
    pattern_day_trader: bool = False
    account_id: str = ""
    account_number: str = ""
    trading_blocked: bool = False

    @property
    def can_trade(self) -> bool:
        return self.status.upper() == "ACTIVE" and not self.trading_blocked


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float
    current_price: float
    unrealized_pl: float = 0.0

    @property
    def side(self) -> str:
        return "long" if self.qty >= 0 else "short"


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str  # "buy" | "sell"
    notional: float | None = None
    qty: float | None = None
    client_order_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"bad side {self.side!r}")
        if (self.notional is None) == (self.qty is None):
            raise ValueError("exactly one of notional/qty must be set")
        if self.notional is not None and self.notional < MIN_NOTIONAL_USD:
            raise ValueError(f"notional {self.notional} below minimum {MIN_NOTIONAL_USD}")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("qty must be positive")


@dataclass(frozen=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    notional: float | None = None
    qty: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    already_existed: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}


@dataclass(frozen=True)
class Clock:
    is_open: bool
    next_open: datetime
    next_close: datetime
    timestamp: datetime


class Broker(Protocol):
    is_paper: bool

    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_clock(self) -> Clock: ...
    def submit_order(self, req: OrderRequest) -> Order: ...
    def get_order(self, order_id: str) -> Order: ...
    def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...
    def list_open_orders(self) -> list[Order]: ...
    def cancel_all_orders(self) -> int: ...
    def close_position(self, symbol: str) -> Order | None: ...
    def is_fractionable(self, symbol: str) -> bool: ...
    def latest_price(self, symbol: str) -> float: ...


# --------------------------------------------------------------------------- secrets

def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_alpaca_keys() -> tuple[str, str]:
    """Return (key_id, secret). Never log these. Raises AuthError if not found."""
    candidates = [dict(os.environ)] + [_parse_env_file(p) for p in KEY_FILES if p.exists()]
    for env in candidates:
        key = env.get("ALPACA_API_KEY") or env.get("APCA_API_KEY_ID")
        sec = env.get("ALPACA_SECRET_KEY") or env.get("APCA_API_SECRET_KEY")
        if key and sec:
            return key, sec
    raise AuthError(
        "Alpaca keys not found. Set ALPACA_API_KEY/ALPACA_SECRET_KEY in the environment, "
        f"or in one of: {', '.join(str(p) for p in KEY_FILES)}. Generate PAPER keys at "
        "https://app.alpaca.markets/paper/dashboard/overview"
    )


def live_gate_open() -> bool:
    if os.environ.get(LIVE_ENV_FLAG) != "1":
        return False
    try:
        return LIVE_CONFIRM_PHRASE in LIVE_CONFIRM_FILE.read_text()
    except OSError:
        return False


# --------------------------------------------------------------------------- helpers

def make_client_order_id(run_date: str, symbol: str, side: str, salt: str = "") -> str:
    """Deterministic per (day, symbol, side) so a re-run cannot double-submit."""
    base = f"cp-{run_date}-{symbol}-{side}{('-' + salt) if salt else ''}"
    return base[:48]


def round_qty(qty: float) -> float:
    factor = 10 ** QTY_DECIMALS
    return int(qty * factor) / factor


# --------------------------------------------------------------------------- Alpaca

class AlpacaBroker:
    """Thin, typed wrapper over alpaca-py. All methods raise BrokerError subclasses."""

    def __init__(self, live: bool = False) -> None:
        if live and not live_gate_open():
            raise LiveTradingBlocked(
                f"Live trading requested but gate is closed. Requires env {LIVE_ENV_FLAG}=1 AND "
                f"{LIVE_CONFIRM_FILE} containing '{LIVE_CONFIRM_PHRASE}'."
            )
        key, secret = load_alpaca_keys()
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        self._trading = TradingClient(key, secret, paper=not live)
        self._data = StockHistoricalDataClient(key, secret)
        self.is_paper = not live
        raw_base = getattr(self._trading, "_base_url", "")
        base = str(getattr(raw_base, "value", raw_base))
        if self.is_paper and base and "paper-api" not in base:
            raise LiveTradingBlocked(f"Expected paper base URL, got {base}")
        if not self.is_paper and "paper-api" in base:
            raise BrokerError("live requested but client points at paper URL")

    # -- wrappers -----------------------------------------------------------
    @staticmethod
    def _wrap(exc: Exception) -> BrokerError:
        msg = str(exc)
        code = getattr(exc, "status_code", None)
        if code in (401, 403) or "unauthorized" in msg.lower() or "forbidden" in msg.lower():
            return AuthError(
                "Alpaca rejected the API keys (401/403). Paper keys are separate from live keys "
                "and can be regenerated at https://app.alpaca.markets/paper/dashboard/overview."
            )
        return BrokerError(msg)

    def get_account(self) -> Account:
        try:
            a = self._trading.get_account()
        except Exception as exc:
            raise self._wrap(exc) from exc
        return Account(
            equity=float(a.equity), cash=float(a.cash), buying_power=float(a.buying_power),
            last_equity=float(a.last_equity), status=str(getattr(a.status, "value", a.status)),
            is_paper=self.is_paper, pattern_day_trader=bool(getattr(a, "pattern_day_trader", False) or False),
            account_id=str(a.id), account_number=str(getattr(a, "account_number", "") or ""),
            trading_blocked=bool(getattr(a, "trading_blocked", False) or getattr(a, "account_blocked", False)
                                 or getattr(a, "trade_suspended_by_user", False)),
        )

    def get_positions(self) -> list[Position]:
        try:
            ps = self._trading.get_all_positions()
        except Exception as exc:
            raise self._wrap(exc) from exc
        return [
            Position(symbol=p.symbol, qty=float(p.qty), market_value=float(p.market_value),
                     avg_entry_price=float(p.avg_entry_price), current_price=float(p.current_price),
                     unrealized_pl=float(p.unrealized_pl or 0.0))
            for p in ps
        ]

    def get_clock(self) -> Clock:
        try:
            c = self._trading.get_clock()
        except Exception as exc:
            raise self._wrap(exc) from exc
        return Clock(is_open=bool(c.is_open), next_open=c.next_open, next_close=c.next_close, timestamp=c.timestamp)

    @staticmethod
    def _to_order(o) -> Order:
        return Order(
            id=str(o.id), client_order_id=str(o.client_order_id), symbol=o.symbol,
            side=str(getattr(o.side, "value", o.side)), status=str(getattr(o.status, "value", o.status)),
            notional=float(o.notional) if o.notional is not None else None,
            qty=float(o.qty) if o.qty is not None else None,
            filled_qty=float(o.filled_qty or 0.0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
            submitted_at=o.submitted_at,
        )

    def submit_order(self, req: OrderRequest) -> Order:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        cid = req.client_order_id or make_client_order_id(datetime.now(timezone.utc).strftime("%Y%m%d"), req.symbol, req.side, uuid.uuid4().hex[:6])
        kwargs = dict(symbol=req.symbol, side=OrderSide.BUY if req.side == "buy" else OrderSide.SELL,
                      time_in_force=TimeInForce.DAY, client_order_id=cid)
        if req.notional is not None:
            kwargs["notional"] = round(req.notional, 2)
        else:
            kwargs["qty"] = round_qty(req.qty)
        # Idempotency: look up by client id first (any status counts as already placed).
        try:
            existing = self.get_order_by_client_id(cid)
        except BrokerError:
            existing = None
        if existing is not None:
            return replace(existing, already_existed=True)
        try:
            o = self._trading.submit_order(MarketOrderRequest(**kwargs))
        except Exception as exc:
            if "client_order_id" in str(exc) and "unique" in str(exc).lower():
                existing = self.get_order_by_client_id(cid)
                if existing is not None:
                    return replace(existing, already_existed=True)
            raise self._wrap(exc) from exc
        return self._to_order(o)

    def get_order(self, order_id: str) -> Order:
        try:
            return self._to_order(self._trading.get_order_by_id(order_id))
        except Exception as exc:
            raise self._wrap(exc) from exc

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            return self._to_order(self._trading.get_order_by_client_id(client_order_id))
        except Exception as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise self._wrap(exc) from exc

    def list_open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            os_ = self._trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        except Exception as exc:
            raise self._wrap(exc) from exc
        return [self._to_order(o) for o in os_]

    def cancel_all_orders(self) -> int:
        try:
            res = self._trading.cancel_orders()
        except Exception as exc:
            raise self._wrap(exc) from exc
        return len(res or [])

    def close_position(self, symbol: str) -> Order | None:
        try:
            o = self._trading.close_position(symbol)
        except Exception as exc:
            if "404" in str(exc) or "position does not exist" in str(exc).lower():
                return None
            raise self._wrap(exc) from exc
        return self._to_order(o)

    def is_fractionable(self, symbol: str) -> bool:
        try:
            a = self._trading.get_asset(symbol)
        except Exception as exc:
            raise self._wrap(exc) from exc
        return bool(getattr(a, "fractionable", False)) and bool(getattr(a, "tradable", False))

    def latest_price(self, symbol: str) -> float:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        try:
            t = self._data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        except Exception as exc:
            raise self._wrap(exc) from exc
        return float(t[symbol].price)


# --------------------------------------------------------------------------- Fake

class FakeBroker:
    """Deterministic in-memory broker for tests, dry-runs and replay. Fills market orders
    immediately at `prices[symbol]` (or the price passed to set_prices)."""

    def __init__(self, cash: float = 100_000.0, prices: dict[str, float] | None = None,
                 positions: dict[str, float] | None = None, market_open: bool = True,
                 is_paper: bool = True, fractionable: set[str] | None = None, account_id: str = "fake") -> None:
        self.is_paper = is_paper
        self._account_id = account_id
        self._cash = float(cash)
        self._prices = dict(prices or {})
        self._qty = dict(positions or {})
        self._orders: dict[str, Order] = {}
        self._by_cid: dict[str, str] = {}
        self._market_open = market_open
        self._fractionable = fractionable
        self.submitted: list[OrderRequest] = []
        self._peak_equity = self.get_account().equity

    def set_prices(self, prices: dict[str, float]) -> None:
        self._prices = {**self._prices, **prices}

    def get_account(self) -> Account:
        eq = self._cash + sum(q * self._prices.get(s, 0.0) for s, q in self._qty.items())
        return Account(equity=eq, cash=self._cash, buying_power=self._cash, last_equity=eq,
                       status="ACTIVE", is_paper=self.is_paper, account_id=self._account_id)

    def get_positions(self) -> list[Position]:
        return [Position(symbol=s, qty=q, market_value=q * self._prices.get(s, 0.0), avg_entry_price=self._prices.get(s, 0.0),
                         current_price=self._prices.get(s, 0.0)) for s, q in self._qty.items() if abs(q) > 1e-9]

    def get_clock(self) -> Clock:
        now = datetime.now(timezone.utc)
        return Clock(is_open=self._market_open, next_open=now, next_close=now, timestamp=now)

    def submit_order(self, req: OrderRequest) -> Order:
        cid = req.client_order_id or f"fake-{uuid.uuid4().hex[:8]}"
        if cid in self._by_cid:
            return replace(self._orders[self._by_cid[cid]], already_existed=True)
        price = self._prices.get(req.symbol)
        if price is None:
            raise BrokerError(f"no price for {req.symbol}")
        qty = req.qty if req.qty is not None else req.notional / price
        if self._fractionable is not None and req.symbol not in self._fractionable:
            qty = float(int(qty))
        signed = qty if req.side == "buy" else -qty
        cost = signed * price
        if req.side == "buy" and cost > self._cash + 1e-6:
            raise BrokerError("insufficient buying power")
        self._cash -= cost
        self._qty[req.symbol] = self._qty.get(req.symbol, 0.0) + signed
        oid = uuid.uuid4().hex
        order = Order(id=oid, client_order_id=cid, symbol=req.symbol, side=req.side, status="filled",
                      notional=req.notional, qty=qty, filled_qty=qty, filled_avg_price=price,
                      submitted_at=datetime.now(timezone.utc))
        self._orders[oid] = order
        self._by_cid[cid] = oid
        self.submitted.append(req)
        return order

    def get_order(self, order_id: str) -> Order:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise BrokerError(f"unknown order {order_id}") from exc

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        oid = self._by_cid.get(client_order_id)
        return self._orders.get(oid) if oid else None

    def list_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if not o.is_terminal]

    def cancel_all_orders(self) -> int:
        return 0

    def close_position(self, symbol: str) -> Order | None:
        qty = self._qty.get(symbol, 0.0)
        if abs(qty) < 1e-9:
            return None
        return self.submit_order(OrderRequest(symbol=symbol, side="sell" if qty > 0 else "buy", qty=abs(qty),
                                              client_order_id=f"close-{symbol}-{uuid.uuid4().hex[:6]}"))

    def is_fractionable(self, symbol: str) -> bool:
        return True if self._fractionable is None else symbol in self._fractionable

    def latest_price(self, symbol: str) -> float:
        try:
            return self._prices[symbol]
        except KeyError as exc:
            raise BrokerError(f"no price for {symbol}") from exc
