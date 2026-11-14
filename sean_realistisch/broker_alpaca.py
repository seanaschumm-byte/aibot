"""alpaca-py wrapper: retries on rate limits / server errors, market-hours check, fractional
(notional) orders, day stop orders, daily bars."""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopOrderRequest

from . import config as C

log = logging.getLogger("sean_realistisch")
ET = ZoneInfo("America/New_York")


def retry(fn, *args, **kwargs):
    delay = 2.0
    for attempt in range(1, C.API_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            code = getattr(e, "status_code", None)
            if code not in (429, 500, 502, 503, 504) or attempt == C.API_RETRIES:
                raise
            log.warning("Alpaca %s, retry %d in %.0fs", code, attempt, delay)
        except (ConnectionError, TimeoutError, OSError) as e:
            if attempt == C.API_RETRIES:
                raise
            log.warning("network problem (%s), retry %d in %.0fs", type(e).__name__, attempt, delay)
        time.sleep(delay)
        delay = min(delay * 2, 60)


class Broker:
    def __init__(self):
        key, secret = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
        self.trading = TradingClient(key, secret, paper=C.PAPER)
        self.data = StockHistoricalDataClient(key, secret)

    # ------------------------------------------------------------ market / account
    def market_open_now(self) -> tuple[bool, str]:
        clock = retry(self.trading.get_clock)
        if not clock.is_open:
            return False, f"market closed (next open {clock.next_open})"
        now = dt.datetime.now(ET).time()
        if not (C.ENTRY_WINDOW[0] <= now <= C.ENTRY_WINDOW[1]):
            return False, f"outside the liquid window {C.ENTRY_WINDOW[0]}-{C.ENTRY_WINDOW[1]} New York time"
        return True, "open"

    def account_equity(self) -> tuple[float, float]:
        a = retry(self.trading.get_account)
        return float(a.equity), float(a.cash)

    def cash_deposits_since(self, since: dt.date) -> list[tuple[str, float]]:
        """Real deposits (activity type CSD), for CAPITAL_MODE 'account'."""
        try:
            acts = retry(self.trading.get, "/account/activities/CSD", {"after": since.isoformat()})
        except APIError:
            return []
        return [(a.get("date") or a.get("transaction_time", "")[:10], float(a.get("net_amount", 0))) for a in acts or []]

    # ------------------------------------------------------------ data
    def daily_bars(self, symbols: list[str], days: int = 420) -> dict[str, pd.DataFrame]:
        req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                               start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days),
                               feed=DataFeed.IEX, adjustment=Adjustment.ALL)
        df = retry(self.data.get_stock_bars, req).df
        out = {}
        for s in symbols:
            if s in df.index.get_level_values(0):
                d = df.xs(s, level=0)[["open", "high", "low", "close", "volume"]].sort_index()
                # keep completed days only: today's bar is still forming during the session
                today = dt.datetime.now(ET).date()
                d = d[[ts.astimezone(ET).date() < today for ts in d.index]]
                out[s] = d
        return out

    def price(self, symbol: str) -> float:
        r = retry(self.data.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))
        return float(r[symbol].price)

    # ------------------------------------------------------------ orders
    def my_orders(self, since: dt.date) -> list:
        """All orders of this bot (client_order_id prefix), oldest first."""
        out, after = [], dt.datetime.combine(since - dt.timedelta(days=1), dt.time(), dt.timezone.utc)
        seen = set()
        while True:
            batch = retry(self.trading.get_orders, GetOrdersRequest(status=QueryOrderStatus.ALL, after=after,
                                                                    direction="asc", limit=500))
            new = [o for o in batch if o.id not in seen]
            for o in new:
                seen.add(o.id)
                if (o.client_order_id or "").startswith(C.PREFIX + "-"):
                    out.append(o)
            if len(batch) < 500 or not new:
                return out
            after = batch[-1].submitted_at

    def buy_notional(self, symbol: str, usd: float, cid: str):
        return retry(self.trading.submit_order, MarketOrderRequest(symbol=symbol, notional=round(usd, 2),
                     side=OrderSide.BUY, time_in_force=TimeInForce.DAY, client_order_id=cid))

    def sell_qty(self, symbol: str, qty: float, cid: str):
        return retry(self.trading.submit_order, MarketOrderRequest(symbol=symbol, qty=round(qty, 6),
                     side=OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=cid))

    def wait_filled(self, order_id, secs: int = 30):
        o = None
        for _ in range(secs):
            o = retry(self.trading.get_order_by_id, order_id)
            if str(o.status).lower().endswith(("filled", "canceled", "rejected", "expired")) and \
                    not str(o.status).lower().endswith("partially_filled"):
                break
            time.sleep(1)
        return o

    def open_stops(self, symbol: str) -> list:
        orders = retry(self.trading.get_orders, GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
        return [o for o in orders if (o.client_order_id or "").startswith(f"{C.PREFIX}-S-")]

    def place_stop(self, symbol: str, qty: float, stop: float, cid: str):
        """Fractional shares only allow DAY stop orders, so the bot re-places them every session."""
        return retry(self.trading.submit_order, StopOrderRequest(symbol=symbol, qty=round(qty, 6), side=OrderSide.SELL,
                     time_in_force=TimeInForce.DAY, stop_price=round(stop, 2), client_order_id=cid))

    def cancel_stops(self, symbol: str) -> None:
        for o in self.open_stops(symbol):
            try:
                retry(self.trading.cancel_order_by_id, o.id)
            except APIError:
                pass
        time.sleep(1)
