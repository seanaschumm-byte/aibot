"""Thin ccxt wrapper: retries on rate limits / network drops, isolated leverage, stops."""
from __future__ import annotations

import logging
import os
import time

import ccxt
import pandas as pd

from . import config as C

log = logging.getLogger("sean_lavarge")
RETRYABLE = (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable,
             ccxt.RequestTimeout, ccxt.DDoSProtection)


def retry(fn, *args, **kwargs):
    delay = 2.0
    for attempt in range(1, C.API_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except RETRYABLE as e:
            if attempt == C.API_RETRIES:
                raise
            log.warning("API problem (%s), retry %d/%d in %.0fs", type(e).__name__, attempt,
                        C.API_RETRIES, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


class Exchange:
    def __init__(self):
        klass = getattr(ccxt, C.EXCHANGE_ID)
        self.ex = klass({"apiKey": os.getenv("EXCHANGE_API_KEY", ""),
                         "secret": os.getenv("EXCHANGE_SECRET", ""),
                         "enableRateLimit": True,
                         "options": {"defaultType": "swap"}})
        if C.TESTNET:
            self.ex.set_sandbox_mode(True)
        retry(self.ex.load_markets)

    # ------------------------------------------------------------ data
    def ohlcv(self, symbol: str, limit: int = 300) -> pd.DataFrame:
        rows = retry(self.ex.fetch_ohlcv, symbol, C.TIMEFRAME, None, limit)
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts")
        # drop the still-forming candle
        tf_ms = self.ex.parse_timeframe(C.TIMEFRAME) * 1000
        now = pd.Timestamp.now(tz="UTC")
        if len(df) and df.index[-1] + pd.Timedelta(milliseconds=tf_ms) > now:
            df = df.iloc[:-1]
        return df

    def price(self, symbol: str) -> float:
        t = retry(self.ex.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close"))

    def equity(self) -> tuple[float, float]:
        """(total equity, free margin) in the quote currency."""
        bal = retry(self.ex.fetch_balance)
        total = bal.get("total", {}).get(C.QUOTE) or 0.0
        free = bal.get("free", {}).get(C.QUOTE) or 0.0
        return float(total), float(free)

    # ------------------------------------------------------------ positions
    def position(self, symbol: str) -> dict | None:
        for p in retry(self.ex.fetch_positions, [symbol]) or []:
            contracts = float(p.get("contracts") or 0)
            if p.get("symbol") == symbol and contracts > 0:
                size = contracts * float(p.get("contractSize") or 1)
                return {"side": p.get("side"), "amount": size,
                        "entry": float(p.get("entryPrice") or 0),
                        "unrealized": float(p.get("unrealizedPnl") or 0)}
        return None

    def setup_leverage(self, symbol: str, leverage: int) -> None:
        """Isolated margin + leverage. On Kraken Futures a per-contract leverage preference IS
        isolated margin; on other exchanges set the margin mode explicitly."""
        if self.ex.has.get("setMarginMode"):
            try:
                retry(self.ex.set_margin_mode, "isolated", symbol)
            except ccxt.ExchangeError as e:          # often "already isolated"
                log.info("margin mode: %s", e)
        try:
            retry(self.ex.set_leverage, leverage, symbol)
        except ccxt.ExchangeError as e:
            log.info("leverage: %s", e)

    def _contracts(self, symbol: str, amount: float) -> float:
        cs = float(self.ex.market(symbol).get("contractSize") or 1)
        return float(self.ex.amount_to_precision(symbol, amount / cs))

    def min_amount(self, symbol: str) -> float:
        m = self.ex.market(symbol)
        cs = float(m.get("contractSize") or 1)
        return float((m.get("limits", {}).get("amount", {}) or {}).get("min") or 0) * cs

    def market(self, symbol: str, side: str, amount: float, reduce_only: bool = False) -> dict:
        params = {"reduceOnly": True} if reduce_only else {}
        return retry(self.ex.create_order, symbol, "market", side,
                     self._contracts(symbol, amount), None, params)

    def place_stop(self, symbol: str, pos_side: str, amount: float, stop: float) -> dict:
        """Hard stop on the exchange: reduce-only stop-market order."""
        side = "sell" if pos_side == "long" else "buy"
        price = float(self.ex.price_to_precision(symbol, stop))
        return retry(self.ex.create_order, symbol, "market", side, self._contracts(symbol, amount),
                     None, {"triggerPrice": price, "stopPrice": price, "reduceOnly": True})

    def cancel_stops(self, symbol: str) -> None:
        for o in retry(self.ex.fetch_open_orders, symbol) or []:
            if o.get("reduceOnly") or o.get("triggerPrice") or o.get("stopPrice"):
                try:
                    retry(self.ex.cancel_order, o["id"], symbol)
                except ccxt.OrderNotFound:
                    pass

    def has_stop(self, symbol: str) -> bool:
        return any(o.get("triggerPrice") or o.get("stopPrice")
                   for o in retry(self.ex.fetch_open_orders, symbol) or [])

    def realized_since(self, symbol: str, since_ms: int) -> float | None:
        """Realised PnL of fills since a time (None if the exchange doesn't report it)."""
        try:
            trades = retry(self.ex.fetch_my_trades, symbol, since_ms)
        except ccxt.NotSupported:
            return None
        pnl = [t.get("info", {}).get("realizedPnl") or t.get("info", {}).get("realized_pnl")
               for t in trades]
        vals = [float(x) for x in pnl if x not in (None, "")]
        fees = sum(float((t.get("fee") or {}).get("cost") or 0) for t in trades)
        return (sum(vals) - fees) if vals else None
