"""Sean Lavarge on Alpaca: the same Turtle breakout signals on 24/7 BTC/ETH/SOL 4h candles,
executed with leveraged / inverse crypto ETFs during US market hours.

  long  BTC -> BITU (2x)   ETH -> ETHT (2x)   SOL -> SOLZ (1x; the 2x SOLT isn't fractionable)
  short BTC -> SBIT (-2x)  ETH -> ETHD (-2x)  SOL -> no inverse ETF, so no SOL shorts

Effective leverage = ETF leverage x account margin (max MARGIN_MULTIPLIER, 2x like Reg-T overnight),
capped at config.MAX_LEVERAGE. Risk 1.5% of risk equity per trade, measured on the underlying's
2-ATR stop translated to the ETF (ETF move ~ leverage x underlying move).

  python -m sean_lavarge.alpaca_main          one cycle (GitHub Actions every 15 min)
  python -m sean_lavarge.alpaca_main --loop   run forever on your PC
Orders carry the client_order_id prefix "sl-". State: state/alpaca_state.json.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
from zoneinfo import ZoneInfo

import pandas as pd

from . import config as C
from . import notifier
from . import risk_manager as R
from .strategy import add_indicators, entry_signal, exit_signal

log = logging.getLogger("sean_lavarge")
ET = ZoneInfo("America/New_York")
PREFIX = "sl"
STATE_FILE = os.path.join(os.path.dirname(C.STATE_FILE), "alpaca_state.json")
MARGIN_MULTIPLIER = float(os.getenv("MARGIN_MULTIPLIER", "2.0"))
SIGNAL_LOOKBACK_BARS = 3          # a breakout from the last 12h still counts if price is still beyond it
TRADE_WINDOW = (dt.time(9, 35), dt.time(15, 55))

# underlying -> (long ETF, its leverage, short ETF or None, its leverage)
MAP = {
    "BTC/USD": ("BITU", 2.0, "SBIT", 2.0),
    "ETH/USD": ("ETHT", 2.0, "ETHD", 2.0),
    "SOL/USD": ("SOLZ", 1.0, None, 0.0),
}


# ---------------------------------------------------------------- broker
class AlpacaBroker:
    def __init__(self):
        from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
        from alpaca.trading.client import TradingClient
        key, sec = os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")
        self.t = TradingClient(key, sec, paper=os.getenv("PAPER", "true").lower() != "false")
        self.crypto = CryptoHistoricalDataClient(key, sec)
        self.stocks = StockHistoricalDataClient(key, sec)

    def _retry(self, fn, *a, **k):
        from alpaca.common.exceptions import APIError
        delay = 2.0
        for i in range(1, C.API_RETRIES + 1):
            try:
                return fn(*a, **k)
            except APIError as e:
                if getattr(e, "status_code", None) not in (429, 500, 502, 503, 504) or i == C.API_RETRIES:
                    raise
            except (ConnectionError, TimeoutError, OSError):
                if i == C.API_RETRIES:
                    raise
            log.warning("Alpaca busy / network problem, retry %d in %.0fs", i, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)

    def market_open(self) -> tuple[bool, str]:
        c = self._retry(self.t.get_clock)
        if not c.is_open:
            return False, f"US market closed (next open {c.next_open})"
        now = dt.datetime.now(ET).time()
        if not (TRADE_WINDOW[0] <= now <= TRADE_WINDOW[1]):
            return False, "outside the liquid window"
        return True, "open"

    def bars_4h(self, symbol: str) -> pd.DataFrame:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame(4, TimeFrameUnit.Hour),
                                start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60))
        df = self._retry(self.crypto.get_crypto_bars, req).df.xs(symbol, level=0)
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        if len(df) and df.index[-1] + pd.Timedelta(hours=4) > pd.Timestamp.now(tz="UTC"):
            df = df.iloc[:-1]                                    # drop the forming candle
        return df

    def crypto_price(self, symbol: str) -> float:
        from alpaca.data.requests import CryptoLatestTradeRequest
        return float(self._retry(self.crypto.get_crypto_latest_trade,
                                 CryptoLatestTradeRequest(symbol_or_symbols=symbol))[symbol].price)

    def etf_price(self, symbol: str) -> float:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        return float(self._retry(self.stocks.get_stock_latest_trade,
                                 StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol].price)

    def my_orders(self) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        out, seen = [], set()
        after = dt.datetime.combine(C.START_DATE - dt.timedelta(days=1), dt.time(), dt.timezone.utc)
        while True:
            batch = self._retry(self.t.get_orders, GetOrdersRequest(status=QueryOrderStatus.ALL, after=after,
                                                                    direction="asc", limit=500))
            new = [o for o in batch if o.id not in seen]
            for o in new:
                seen.add(o.id)
                if (o.client_order_id or "").startswith(PREFIX + "-") and float(o.filled_qty or 0) > 0:
                    out.append({"symbol": o.symbol, "side": str(o.side).lower().split(".")[-1],
                                "qty": float(o.filled_qty), "price": float(o.filled_avg_price),
                                "time": str(o.filled_at or o.submitted_at)})
            if len(batch) < 500 or not new:
                return out
            after = batch[-1].submitted_at

    def buy(self, symbol: str, usd: float):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        o = self._retry(self.t.submit_order, MarketOrderRequest(
            symbol=symbol, notional=round(usd, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            client_order_id=f"{PREFIX}-B-{symbol}-{int(time.time())}"))
        return self._wait(o.id)

    def sell(self, symbol: str, qty: float):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        self.cancel_stops(symbol)
        o = self._retry(self.t.submit_order, MarketOrderRequest(
            symbol=symbol, qty=int(qty * 1e6) / 1e6, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            client_order_id=f"{PREFIX}-X-{symbol}-{int(time.time())}"))
        return self._wait(o.id)

    def _wait(self, oid):
        o = None
        for _ in range(30):
            o = self._retry(self.t.get_order_by_id, oid)
            st = str(o.status).lower()
            if st.endswith(("filled", "canceled", "rejected", "expired")) and not st.endswith("partially_filled"):
                break
            time.sleep(1)
        return o

    def stops(self, symbol: str) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return [o for o in self._retry(self.t.get_orders, GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
                if (o.client_order_id or "").startswith(f"{PREFIX}-S-")]

    def cancel_stops(self, symbol: str) -> None:
        for o in self.stops(symbol):
            try:
                self._retry(self.t.cancel_order_by_id, o.id)
            except Exception:
                pass
        time.sleep(1)

    def ensure_stop(self, symbol: str, qty: float, stop: float) -> None:
        """Fractional shares only allow DAY stops: re-placed every session."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        q, sp = int(qty * 1e6) / 1e6, round(stop, 2)
        cur = self.stops(symbol)
        if len(cur) == 1 and abs(float(cur[0].stop_price) - sp) < 0.011 and abs(float(cur[0].qty) - q) < 1e-5:
            return
        self.cancel_stops(symbol)
        self._retry(self.t.submit_order, StopOrderRequest(symbol=symbol, qty=q, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, stop_price=sp, client_order_id=f"{PREFIX}-S-{symbol}-{int(time.time())}"))


# ---------------------------------------------------------------- pure helpers
def etf_stop(etf_entry: float, lev: float, stop_pct: float) -> float:
    """ETF price where the underlying's stop is hit (ETF moves ~ lev x the underlying)."""
    return etf_entry * (1 - lev * stop_pct)


def etf_value(risk_eq: float, stop_pct: float, lev: float, cash_room: float) -> tuple[float, bool]:
    """ETF dollars so that hitting the stop loses 1.5% of risk equity; capped by the exposure
    limit (MAX_LEVERAGE x equity / ETF leverage), the margin limit and available buying power."""
    if risk_eq <= 0 or stop_pct <= 0 or lev <= 0:
        return 0.0, False
    value = risk_eq * C.RISK_PER_TRADE / (lev * stop_pct)
    cap = min(C.MAX_LEVERAGE * risk_eq / lev, MARGIN_MULTIPLIER * risk_eq, cash_room)
    return max(0.0, min(value, cap)), value > cap


def ledger(orders: list[dict], deposited_usd: float) -> dict:
    cash, qty, closes = deposited_usd, {}, []
    cost: dict[str, float] = {}
    for o in orders:
        s = o["symbol"]
        if o["side"] == "buy":
            cash -= o["qty"] * o["price"]
            qty[s] = qty.get(s, 0) + o["qty"]
            cost[s] = cost.get(s, 0) + o["qty"] * o["price"]
        else:
            cash += o["qty"] * o["price"]
            share = min(1.0, o["qty"] / qty[s]) if qty.get(s) else 1.0
            part = cost.get(s, 0) * share
            qty[s] = qty.get(s, 0) - o["qty"]
            cost[s] = cost.get(s, 0) - part
            if qty[s] <= 1e-6:
                closes.append({"symbol": s, "pnl": o["qty"] * o["price"] - part + 0.0, "time": o["time"]})
                qty.pop(s), cost.pop(s, None)
    return {"cash": cash, "qty": qty, "closes": closes}


def recent_signal(df: pd.DataFrame, price: float) -> tuple[str | None, pd.Series | None]:
    """Breakout on one of the last few closed 4h bars, with price still beyond that bar's channel."""
    for i in range(1, SIGNAL_LOOKBACK_BARS + 1):
        row = df.iloc[-i]
        sig = entry_signal(row)
        if sig == "long" and price > row["don_hi"] and price > row["ema"]:
            return sig, row
        if sig == "short" and price < row["don_lo"] and price < row["ema"]:
            return sig, row
    return None, None


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"positions": {}, "closed": [], "equity_hist": [], "paused_until": None,
                "last_weekly": None, "last_bar": {}}


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(s, f, indent=1, default=str)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ---------------------------------------------------------------- one cycle
def run_once(b, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = load_state()
    deps = R.scheduled_deposits(now)
    deposited = sum(a for _, a in deps) * C.EURUSD
    led = ledger(b.my_orders(), deposited)
    etf_px = {e: b.etf_price(e) for e in led["qty"]}
    equity = led["cash"] + sum(q * etf_px[e] for e, q in led["qty"].items())
    risk_eq = equity - R.locked_amount(deps, now) * C.EURUSD
    exposure = sum(led["qty"][p["etf"]] * etf_px[p["etf"]] for p in s["positions"].values() if p["etf"] in led["qty"])
    cash_room = max(0.0, MARGIN_MULTIPLIER * risk_eq - exposure)

    # positions closed by an ETF stop since the last run
    for u, p in list(s["positions"].items()):
        if p["etf"] not in led["qty"]:
            pnl = sum(c["pnl"] for c in led["closes"] if c["symbol"] == p["etf"] and c["time"] >= p["opened"])
            s["closed"].append({"underlying": u, "etf": p["etf"], "dir": p["dir"], "pnl": round(pnl, 2),
                                "opened": p["opened"], "closed": now.isoformat()})
            s["positions"].pop(u)
            notifier.send(f"CLOSED {p['etf']} ({p['dir']} {u}): P&L ${pnl:+.2f}")

    s["equity_hist"] = [e for e in s["equity_hist"] if dt.datetime.fromisoformat(e[0]) >= now - dt.timedelta(hours=48)]
    s["equity_hist"].append([now.isoformat(), equity])
    wk = f"{now.isocalendar()[0]}W{now.isocalendar()[1]:02d}"
    if s.get("last_weekly") != wk:
        s["last_weekly"] = wk
        notifier.send(f"Weekly update {wk}: equity ${equity:.2f} (deposited ${deposited:.2f}), "
                      f"open {len(s['positions'])}, closed trades {len(s['closed'])}")
    log.info("equity $%.2f, risk equity $%.2f, exposure $%.2f, room $%.2f", equity, risk_eq, exposure, cash_room)

    open_now, why = b.market_open()
    paused = bool(s.get("paused_until") and dt.datetime.fromisoformat(s["paused_until"]) > now)
    if not paused:
        window = [e for t, e in s["equity_hist"] if dt.datetime.fromisoformat(t) >= now - dt.timedelta(hours=24)]
        hit, reason = R.breaker([{"closed": c["closed"], "pnl": c["pnl"]} for c in s["closed"]],
                                window[0] if window else None, equity, now)
        if hit:
            s["paused_until"] = (now + dt.timedelta(hours=C.PAUSE_HOURS)).isoformat()
            paused = True
            notifier.send(f"CIRCUIT BREAKER: {reason}. No new trades for {C.PAUSE_HOURS}h.")

    for u, (long_etf, long_lev, short_etf, short_lev) in MAP.items():
        df = add_indicators(b.bars_4h(u))
        if len(df) < C.EMA_TREND + 2:
            log.info("%s: not enough 4h history yet", u)
            continue
        row, price = df.iloc[-1], b.crypto_price(u)
        p = s["positions"].get(u)
        if p:
            etf, q = p["etf"], led["qty"].get(p["etf"], 0)
            if not open_now:
                log.info("%s %s via %s: market closed (%s); stop %.2f waits for the open", u, p["dir"], etf, why, p["stop_etf"])
                continue
            ep = b.etf_price(etf)
            under_stop_hit = price <= p["stop_under"] if p["dir"] == "long" else price >= p["stop_under"]
            if exit_signal(row, p["dir"]) or under_stop_hit:
                o = b.sell(etf, q)
                notifier.send(f"{'STOP HIT' if under_stop_hit else 'EXIT (10-bar)'} {etf}: sold {q:.4f} @ "
                              f"{float(o.filled_avg_price or ep):.2f} ({u} {price:.2f})")
                continue
            if not paused and R.pyramid_due(p["under_entry"], p["atr"], price, p["dir"], p["pyramids"]):
                add, _ = etf_value(risk_eq, C.STOP_ATR * p["atr"] / p["under_entry"], p["lev"], cash_room)
                add = min(add, p["first_value"] * C.PYRAMID_FRACTION)
                if add >= 1:
                    o = b.buy(etf, add)
                    q += float(o.filled_qty or 0)
                    p["pyramids"] += 1
                    p["stop_under"], p["stop_etf"] = p["under_entry"], p["etf_entry"]     # breakeven
                    notifier.send(f"PYRAMID {etf} +${add:.2f}; stop to breakeven ({u} {p['stop_under']:.2f})")
            b.ensure_stop(etf, q, p["stop_etf"])
            log.info("%s %s via %s qty %.4f, %s %.2f (stop %.2f), ETF %.2f (stop %.2f)", u, p["dir"], etf, q, u,
                     price, p["stop_under"], ep, p["stop_etf"])
            continue

        sig, srow = recent_signal(df, price)
        log.info("%s: %s (price %.2f, EMA200 %.2f, 20-bar %.2f-%.2f)", u, sig or "no signal", price, row["ema"],
                 row["don_lo"], row["don_hi"])
        if not sig or paused or len(s["positions"]) >= C.MAX_OPEN_POSITIONS:
            continue
        etf, lev = (long_etf, long_lev) if sig == "long" else (short_etf, short_lev)
        if not etf:
            log.info("%s short signal, but there is no inverse ETF: skipped", u)
            continue
        if s["last_bar"].get(u) == str(srow.name):
            continue
        if not open_now:
            log.info("%s %s signal waits for the US market (%s)", u, sig, why)
            continue
        atr = float(srow["atr"])
        stop_pct = C.STOP_ATR * atr / price
        value, capped = etf_value(risk_eq, stop_pct, lev, cash_room)
        if value < 1:
            continue
        o = b.buy(etf, value)
        q, fill = float(o.filled_qty or 0), float(o.filled_avg_price or 0)
        if q <= 0:
            notifier.send(f"BUY {etf} not filled ({o.status})")
            continue
        s["last_bar"][u] = str(srow.name)
        stop_under = R.stop_price(price, atr, sig)
        stop_e = etf_stop(fill, lev, stop_pct)
        b.ensure_stop(etf, q, stop_e)
        s["positions"][u] = {"etf": etf, "lev": lev, "dir": sig, "under_entry": price, "atr": atr,
                             "etf_entry": fill, "first_value": value, "stop_under": stop_under,
                             "stop_etf": stop_e, "pyramids": 0, "opened": now.isoformat()}
        cash_room -= value
        notifier.send(f"ENTRY {sig.upper()} {u} via {etf} ({lev:.0f}x): ${value:.2f} = {q:.4f} @ {fill:.2f}; "
                      f"stop {u} {stop_under:.2f} / {etf} {stop_e:.2f}; risk ~${value * lev * stop_pct:.2f} "
                      f"({C.RISK_PER_TRADE:.1%}), exposure {value * lev / risk_eq:.1f}x" + (" [capped]" if capped else ""))

    save_state(s)
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")):
        log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return
    b = AlpacaBroker()
    while True:
        try:
            run_once(b)
        except Exception as e:
            log.exception("cycle failed")
            notifier.send(f"ERROR (Alpaca): {type(e).__name__}: {e}")
            if "--loop" not in sys.argv:
                raise
        if "--loop" not in sys.argv:
            return
        time.sleep(C.LOOP_MINUTES * 60)


if __name__ == "__main__":
    main()
