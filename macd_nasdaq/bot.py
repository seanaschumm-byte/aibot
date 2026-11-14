"""MACD Nasdaq live bot (Alpaca paper, QQQ). Rules: see strategy.py.

Runs every 30 minutes in US market hours (GitHub Actions). Daily signals (MACD cross, dip) use
completed daily candles and are acted on once per day; the 12/18/25% profit rules are checked on
every run with the live price. Virtual budget: EUR 500 + EUR 100 every week, own ledger from orders
with client_order_id prefix "mn-". Lots are kept in state/state.json.
  python -m macd_nasdaq.bot            (DRY_RUN=1 to only log)
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
from .strategy import Rules, in_dip, lot_action, macd_state

log = logging.getLogger("macd_nasdaq")
ET = ZoneInfo("America/New_York")
SYMBOL, PREFIX = "QQQ", "mn"
START = dt.date(2026, 11, 16)                 # first Monday
START_EUR, WEEKLY_EUR = 500.0, 100.0
R = Rules()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")


def notify(text: str) -> None:
    log.info("ALERT %s", text)
    hook, token, chat = os.getenv("DISCORD_WEBHOOK_URL"), os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    try:
        if hook:
            requests.post(hook, json={"content": f"[MACD Nasdaq] {text}"}, timeout=10)
        if token and chat:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"[MACD Nasdaq] {text}"}, timeout=10)
    except requests.RequestException:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"lots": [], "closed": [], "signal_day": None, "deposit_weeks": []}


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def weeks_since_start(today: dt.date) -> list[str]:
    """ISO weeks with a deposit: the start week (EUR 500) and every week after (EUR 100)."""
    if today < START:
        return []
    out, d = [], START
    while d <= today:
        y, w, _ = d.isocalendar()
        out.append(f"{y}W{w:02d}")
        d += dt.timedelta(days=7)
    return out


def cash_now(fx: float, today: dt.date) -> float:
    weeks = weeks_since_start(today)
    cash = (START_EUR + WEEKLY_EUR * max(0, len(weeks) - 1)) * fx if weeks else 0.0
    after = dt.datetime.combine(START - dt.timedelta(days=1), dt.time(), dt.timezone.utc).isoformat()
    for o in api("GET", "/v2/orders", params={"status": "all", "after": after, "limit": 500, "direction": "asc"}):
        if (o.get("client_order_id") or "").startswith(PREFIX + "-") and float(o.get("filled_qty") or 0) > 0:
            v = float(o["filled_qty"]) * float(o["filled_avg_price"])
            cash += -v if o["side"] == "buy" else v
    return cash


def daily_closes() -> pd.Series:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)).isoformat()
    js = api("GET", "/v2/stocks/bars", base=DATA_URL, params={"symbols": SYMBOL, "timeframe": "1Day", "start": start,
                                                              "limit": 1000, "feed": "iex", "adjustment": "all"})
    bars = js["bars"][SYMBOL]
    s = pd.Series([b["c"] for b in bars], index=[pd.Timestamp(b["t"]).tz_convert(ET).date() for b in bars])
    return s[s.index < dt.datetime.now(ET).date()]          # completed days only


def price() -> float:
    js = api("GET", f"/v2/stocks/{SYMBOL}/trades/latest", base=DATA_URL, params={"feed": "iex"})
    return float(js["trade"]["p"])


def order(side: str, *, usd: float | None = None, qty: float | None = None):
    cid = f"{PREFIX}-{side[0].upper()}-{int(time.time() * 1000)}"
    body = {"symbol": SYMBOL, "side": side, "type": "market", "time_in_force": "day", "client_order_id": cid}
    body.update({"notional": f"{usd:.2f}"} if usd else {"qty": f"{int(qty * 1e6) / 1e6:.6f}"})
    log.info("ORDER %s %s%s", side, body.get("notional") or body.get("qty"), " (dry run)" if DRY_RUN else "")
    if DRY_RUN:
        return {"filled_qty": (usd / price()) if usd else qty, "filled_avg_price": price()}
    o = api("POST", "/v2/orders", json=body)
    for _ in range(30):
        o = api("GET", f"/v2/orders/{o['id']}")
        if o["status"] in ("filled", "canceled", "rejected", "expired"):
            break
        time.sleep(1)
    return o


def buy(s: dict, usd: float, why: str) -> None:
    if usd < 1:
        return
    o = order("buy", usd=usd)
    q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
    if q > 0:
        s["lots"].append({"qty": q, "entry": px, "peak": px, "half": False, "opened": dt.date.today().isoformat(), "why": why})
        notify(f"BUY ${usd:.2f} QQQ ({q:.4f} @ {px:.2f}): {why}")


def sell(s: dict, lot: dict, qty: float, why: str) -> None:
    o = order("sell", qty=qty)
    q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
    if q > 0:
        lot["qty"] -= q
        notify(f"SELL {q:.4f} QQQ @ {px:.2f} ({px / lot['entry'] - 1:+.1%} on this buy): {why}")


def run_once(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = load_state()
    clock = api("GET", "/v2/clock")
    t_et = now.astimezone(ET)
    if not clock["is_open"] or not (dt.time(9, 40) <= t_et.time() <= dt.time(15, 50)):
        log.info("market closed / outside 9:40-15:50 New York: nothing to do")
        return s
    today = t_et.date()
    closes = daily_closes()
    m, dp = macd_state(closes, R), in_dip(closes, R)
    up_now, up_before, dip_now, dip_before = bool(m.iloc[-1]), bool(m.iloc[-2]), bool(dp.iloc[-1]), bool(dp.iloc[-2])
    px = price()
    cash = cash_now(eurusd(), today)
    high3m = closes.iloc[-R.dip_lookback:].max()
    log.info("QQQ %.2f | MACD %s (yesterday: %s) | %.1f%% under 3-month high%s | cash $%.2f | %d open buys",
             px, "UP" if up_now else "down", "UP" if up_before else "down", (1 - closes.iloc[-1] / high3m) * 100,
             " = DIP" if dip_now else "", cash, len(s["lots"]))

    # 1. profit rules on every run (live price)
    for lot in s["lots"]:
        lot["peak"] = max(lot["peak"], px)
        act, _ = lot_action(lot["entry"], px, lot["peak"], lot["half"], R)
        if act == "half":
            sell(s, lot, lot["qty"] * R.tp1_fraction, "+12%: sell half")
            lot["half"] = True
        elif act == "rest":
            sell(s, lot, lot["qty"], "+25% reached" if px >= lot["entry"] * (1 + R.tp2) else "fell back to +18%: profit locked")

    # 2. daily signals, once per day
    if s.get("signal_day") != today.isoformat():
        s["signal_day"] = today.isoformat()
        week = f"{today.isocalendar()[0]}W{today.isocalendar()[1]:02d}"
        cash = cash_now(eurusd(), today)
        if up_before and not up_now and any(l["qty"] > 0 for l in s["lots"]):
            for lot in s["lots"]:
                if lot["qty"] > 0:
                    sell(s, lot, lot["qty"], "MACD sell signal: everything out")
        elif (up_now and not up_before) or (dip_now and not dip_before):
            buy(s, cash * 0.99, "MACD buy signal" if up_now and not up_before else "12% dip under the 3-month high")
            s["deposit_weeks"].append(week)
        elif (up_now or dip_now) and week not in s["deposit_weeks"]:
            buy(s, cash * 0.99, "weekly buy: EUR 100 deposit + money from earlier sales" + (" (in a dip)" if dip_now else ""))
            s["deposit_weeks"].append(week)

    done = [l for l in s["lots"] if l["qty"] <= 1e-6]
    s["closed"] += done
    s["lots"] = [l for l in s["lots"] if l["qty"] > 1e-6]
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
