"""Justin: Bollinger Band bounce on 15 liquid US stocks/ETFs (Alpaca paper).

Chosen on 15 Nov 2026 as the best of 21 known strategies over the previous 6 months
(+EUR 145 on EUR 3,100 deposited, max drawdown -6.3%, 68% winning trades).

Buy:  a stock CLOSES under its lower Bollinger Band (20 days, 2 std): an unusually deep dip.
Sell: the close is back at the 20-day average (middle band), or after 30 trading days.
Max 5 positions, each up to 1/5 of equity; the deepest dips get the free slots first.
Budget (virtual): EUR 500 + EUR 100 every Monday from 16 Nov 2026. Orders: client_order_id "js-".
Acts once per trading day (after 9:45 New York) on yesterday's completed daily candles, like the
backtest (signal on the close -> order the next session).
  python -m justin.bot            (DRY_RUN=1 to only log)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from common import DATA_URL, DRY_RUN, api, eurusd
import journal
from .strategies import STRATEGIES, indicators

log = logging.getLogger("justin")
ET = ZoneInfo("America/New_York")
STRATEGY = "Bollinger Band bounce"
UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA",
            "JPM", "XLE", "GLD"]
MAX_POSITIONS = 5
PREFIX = "js"
START = dt.date(2026, 11, 16)
START_EUR, WEEKLY_EUR = 500.0, 100.0
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")


def notify(text: str) -> None:
    log.info("ALERT %s", text)
    hook, token, chat = os.getenv("DISCORD_WEBHOOK_URL"), os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    try:
        if hook:
            requests.post(hook, json={"content": f"[Justin] {text}"}, timeout=10)
        if token and chat:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"[Justin] {text}"}, timeout=10)
    except requests.RequestException:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"positions": {}, "closed": [], "last_day": None}


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def deposits_usd(today: dt.date, fx: float) -> float:
    if today < START:
        return 0.0
    return (START_EUR + WEEKLY_EUR * ((today - START).days // 7)) * fx


def ledger(fx: float, today: dt.date) -> tuple[float, dict[str, float]]:
    """Cash and holdings of Justin only (from its own orders)."""
    cash, qty = deposits_usd(today, fx), {}
    after = dt.datetime.combine(START - dt.timedelta(days=1), dt.time(), dt.timezone.utc).isoformat()
    for o in api("GET", "/v2/orders", params={"status": "all", "after": after, "limit": 500, "direction": "asc"}):
        if (o.get("client_order_id") or "").startswith(PREFIX + "-") and float(o.get("filled_qty") or 0) > 0:
            q, px = float(o["filled_qty"]), float(o["filled_avg_price"])
            sign = 1 if o["side"] == "buy" else -1
            cash -= sign * q * px
            qty[o["symbol"]] = qty.get(o["symbol"], 0) + sign * q
    return cash, {s: q for s, q in qty.items() if q > 1e-6}


def daily_bars() -> dict[str, pd.DataFrame]:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=420)).isoformat()
    params = {"symbols": ",".join(UNIVERSE), "timeframe": "1Day", "start": start, "limit": 10000,
              "feed": "iex", "adjustment": "all"}
    raw: dict[str, list] = {}
    while True:
        js = api("GET", "/v2/stocks/bars", base=DATA_URL, params=params)
        for s, b in (js.get("bars") or {}).items():
            raw.setdefault(s, []).extend(b)
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    today = dt.datetime.now(ET).date()
    out = {}
    for s, b in raw.items():
        df = pd.DataFrame(b)
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df = df[df["t"].dt.tz_convert(ET).dt.date < today]           # completed sessions only
        out[s] = indicators(df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low",
                                                               "c": "close", "v": "volume"}))
    return out


def order(symbol: str, side: str, *, usd: float | None = None, qty: float | None = None) -> dict:
    body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day",
            "client_order_id": f"{PREFIX}-{side[0].upper()}-{symbol}-{int(time.time() * 1000)}"}
    body.update({"notional": f"{usd:.2f}"} if usd else {"qty": f"{int(qty * 1e6) / 1e6:.6f}"})
    log.info("ORDER %s %s %s%s", side, symbol, body.get("notional") or body.get("qty"), " (dry run)" if DRY_RUN else "")
    if DRY_RUN:
        return {"filled_qty": qty or 0, "filled_avg_price": 0, "status": "dry"}
    o = api("POST", "/v2/orders", json=body)
    for _ in range(30):
        o = api("GET", f"/v2/orders/{o['id']}")
        if o["status"] in ("filled", "canceled", "rejected", "expired"):
            break
        time.sleep(1)
    return o


def run_once(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = load_state()
    t_et = now.astimezone(ET)
    clock = api("GET", "/v2/clock")
    if not clock["is_open"] or t_et.time() < dt.time(9, 45) or t_et.time() > dt.time(15, 50):
        log.info("market closed / outside 9:45-15:50 New York: nothing to do")
        return s
    today = t_et.date()
    if s.get("last_day") == today.isoformat():
        log.info("already traded today")
        return s
    strat = STRATEGIES[STRATEGY]
    fx = eurusd()
    bars = daily_bars()
    cash, held = ledger(fx, today)
    rows = {sym: df.iloc[-1] for sym, df in bars.items() if len(df) > 200}

    # 1. exits (middle band reached or 30 days)
    for sym, q in held.items():
        p = s["positions"].setdefault(sym, {"entry": float(rows[sym]["close"]) if sym in rows else 0, "days": 0,
                                            "opened": today.isoformat()})
        p["days"] += 1
        if sym in rows and strat["exit"](rows[sym], p):
            o = order(sym, "sell", qty=q)
            px = float(o.get("filled_avg_price") or rows[sym]["close"])
            res = px / p["entry"] - 1 if p["entry"] else 0.0
            why = "back at the 20-day average" if rows[sym]["close"] >= rows[sym]["bb_mid"] else "30 days held"
            s["closed"].append({"symbol": sym, "entry": p["entry"], "exit": px, "result": round(res, 4),
                                "opened": p["opened"], "closed": today.isoformat(), "why": why})
            s["positions"].pop(sym)
            cash += q * px
            journal.write("justin", "close", cid=f"{sym}-{p['opened']}", symbol=sym, pnl_pct=round(res, 4), why=why)
            notify(f"SELL {sym} @ {px:.2f} ({res:+.1%}): {why}")
    held_now = [x for x in held if x in s["positions"]]

    # 2. entries: deepest dips first, max 5 positions
    equity = cash + sum(held[x] * float(rows[x]["close"]) for x in held_now if x in rows)
    free = MAX_POSITIONS - len(held_now)
    cands = sorted((strat["rank"](r), sym) for sym, r in rows.items() if sym not in held_now and strat["entry"](r))
    log.info("cash $%.2f, equity $%.2f, %d positions, dips today: %s", cash, equity, len(held_now),
             ", ".join(f"{sym} ({r:.3f})" for r, sym in cands) or "none")
    for _, sym in cands[:max(free, 0)]:
        usd = min(cash, equity / MAX_POSITIONS) * 0.99
        if usd < 1:
            break
        o = order(sym, "buy", usd=usd)
        q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
        if q > 0:
            r = rows[sym]
            s["positions"][sym] = {"entry": px, "days": 0, "opened": today.isoformat()}
            cash -= q * px
            journal.write("justin", "entry", cid=f"{sym}-{today}", symbol=sym, price=px, usd=round(usd, 2),
                          close=float(r["close"]), lower_band=float(r["bb_low"]), middle_band=float(r["bb_mid"]))
            notify(f"BUY {sym} ${usd:.2f} @ {px:.2f}: closed under its lower Bollinger Band "
                   f"({r['close']:.2f} < {r['bb_low']:.2f}); target ~{r['bb_mid']:.2f}")
    s["last_day"] = today.isoformat()
    save_state(s)
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        run_once()
    except Exception as e:
        notify(f"ERROR: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
