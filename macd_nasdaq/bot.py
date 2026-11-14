"""MACD Nasdaq live bot (Alpaca paper, QQQ). Rules: see strategy.py. The same engine runs
Botsi (BTC, ETH, SOL; see botsi/bot.py), which only changes NAME, PREFIX, MARKETS and STATE_FILE.

Every market is its own sleeve with its share of the money (EUR 500 at the start + EUR 100 every
week, split over the markets), and money from its sales goes into its own next buy. Daily signals (MACD cross, 12% dip) use
completed daily candles and are acted on once per day per market; the 12/18/25% profit rules are
checked on every run with the live price. QQQ trades in US market hours only, crypto 24/7.
Orders: client_order_id "mn-<MARKET>-<B|S>-<ms>". Lots in state/state.json.
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
NAME, PREFIX = "MACD Nasdaq", "mn"
START = dt.date(2026, 11, 16)                 # first Monday
START_EUR, WEEKLY_EUR = 500.0, 100.0
MARKETS = {  # tag: (symbol, kind, share of the money, dip look-back in daily bars ~3 months)
    "CORE": ("QQQ", "stock", 0.6, 63),   # core: buy every week, sell all at +60%, buy back at -30%
    "QQQ": ("QQQ", "stock", 0.4, 63),    # satellite: MACD + 12% dips + 12/18/25% profit rules
}
CORE_TAKE_PROFIT, CORE_BUY_BACK = 0.60, 0.30
PLAIN_MACD = False   # True (Botsi): only MACD in/out, no dip buys and no 12/18/25% profit rules
CRYPTO_FEE = 0.0025
MIN_ORDER = 10.0                              # Alpaca minimum for crypto orders
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")


def notify(text: str) -> None:
    log.info("ALERT %s", text)
    hook, token, chat = os.getenv("DISCORD_WEBHOOK_URL"), os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    try:
        if hook:
            requests.post(hook, json={"content": f"[{NAME}] {text}"}, timeout=10)
        if token and chat:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"[{NAME}] {text}"}, timeout=10)
    except requests.RequestException:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    for tag in MARKETS:
        s.setdefault(tag, {"lots": [], "closed": [], "signal_day": None, "deposit_weeks": []})
    return s


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def week_id(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}W{w:02d}"


def deposits_eur(today: dt.date) -> float:
    if today < START:
        return 0.0
    return START_EUR + WEEKLY_EUR * ((today - START).days // 7)


def all_cash(fx: float, today: dt.date) -> dict[str, float]:
    """Cash per market sleeve: its share of the deposits - its buys + its sales."""
    cash = {tag: deposits_eur(today) * fx * m[2] for tag, m in MARKETS.items()}
    after = dt.datetime.combine(START - dt.timedelta(days=1), dt.time(), dt.timezone.utc).isoformat()
    for o in api("GET", "/v2/orders", params={"status": "all", "after": after, "limit": 500, "direction": "asc"}):
        cid = o.get("client_order_id") or ""
        parts = cid.split("-")
        if cid.startswith(PREFIX + "-") and len(parts) > 2 and parts[1] in cash and float(o.get("filled_qty") or 0) > 0:
            v = float(o["filled_qty"]) * float(o["filled_avg_price"])
            fee = CRYPTO_FEE if "/" in o["symbol"] else 0.0
            cash[parts[1]] += -v if o["side"] == "buy" else v * (1 - fee)
    return cash


def daily_closes(tag: str) -> pd.Series:
    sym, kind, _, _ = MARKETS[tag]
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)).isoformat()
    if kind == "stock":
        js = api("GET", "/v2/stocks/bars", base=DATA_URL, params={"symbols": sym, "timeframe": "1Day", "start": start,
                                                                  "limit": 1000, "feed": "iex", "adjustment": "all"})
        bars = js["bars"][sym]
        s = pd.Series([b["c"] for b in bars], index=[pd.Timestamp(b["t"]).tz_convert(ET).date() for b in bars])
        return s[s.index < dt.datetime.now(ET).date()]                 # completed sessions only
    js = api("GET", "/v1beta3/crypto/us/bars", base=DATA_URL, params={"symbols": sym, "timeframe": "1Day",
                                                                      "start": start, "limit": 1000})
    bars = js["bars"][sym]
    s = pd.Series([b["c"] for b in bars], index=[pd.Timestamp(b["t"]).date() for b in bars])
    return s[s.index < dt.datetime.now(dt.timezone.utc).date()]        # completed UTC days only


def price(tag: str) -> float:
    sym, kind, _, _ = MARKETS[tag]
    if kind == "stock":
        return float(api("GET", f"/v2/stocks/{sym}/trades/latest", base=DATA_URL, params={"feed": "iex"})["trade"]["p"])
    js = api("GET", "/v1beta3/crypto/us/latest/trades", base=DATA_URL, params={"symbols": sym})
    return float(js["trades"][sym]["p"])


def order(tag: str, side: str, *, usd: float | None = None, qty: float | None = None):
    sym, kind, _, _ = MARKETS[tag]
    body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day" if kind == "stock" else "gtc",
            "client_order_id": f"{PREFIX}-{tag}-{side[0].upper()}-{int(time.time() * 1000)}"}
    body.update({"notional": f"{usd:.2f}"} if usd else {"qty": f"{int(qty * 1e9) / 1e9:.9f}"})
    log.info("ORDER %s %s %s%s", tag, side, body.get("notional") or body.get("qty"), " (dry run)" if DRY_RUN else "")
    if DRY_RUN:
        p = price(tag)
        return {"filled_qty": (usd / p) if usd else qty, "filled_avg_price": p}
    o = api("POST", "/v2/orders", json=body)
    for _ in range(30):
        o = api("GET", f"/v2/orders/{o['id']}")
        if o["status"] in ("filled", "canceled", "rejected", "expired"):
            break
        time.sleep(1)
    return o


def buy(s: dict, tag: str, usd: float, why: str, today: dt.date) -> None:
    if usd < MIN_ORDER:
        log.info("%s: $%.2f is below the $10 minimum, waiting", tag, usd)
        return
    o = order(tag, "buy", usd=usd)
    q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
    if q > 0:
        if MARKETS[tag][1] == "crypto":
            q *= 1 - CRYPTO_FEE                                        # fee is taken from the coins
        s[tag]["lots"].append({"qty": q, "entry": px, "peak": px, "half": False, "opened": today.isoformat(), "why": why})
        notify(f"BUY {tag} ${usd:.2f} ({q:.6g} @ {px:,.2f}): {why}")


def sell(tag: str, lot: dict, qty: float, why: str) -> None:
    px_now = price(tag)
    if qty * px_now < MIN_ORDER:                                       # too small to split: sell the whole lot
        qty = lot["qty"]
    o = order(tag, "sell", qty=qty)
    q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
    if q > 0:
        lot["qty"] -= q
        notify(f"SELL {tag} {q:.6g} @ {px:,.2f} ({px / lot['entry'] - 1:+.1%} on this buy): {why}")


def run_core(s: dict, tag: str, cash: float, today: dt.date) -> None:
    """Core: buy every week and hold; sell everything at +60% on the average cost; after that,
    buy back with all core cash once the price is 30% under the highest price since the sale."""
    st = s[tag]
    st.setdefault("sold", None)
    px = price(tag)
    held = [l for l in st["lots"] if l["qty"] > 0]
    if st["sold"] is None:
        avg = sum(l["qty"] * l["entry"] for l in held) / sum(l["qty"] for l in held) if held else 0.0
        log.info("CORE QQQ %.2f | avg cost %.2f (%+.1f%%, sells at +60%%) | cash $%.2f", px, avg,
                 (px / avg - 1) * 100 if avg else 0, cash)
        if held and px >= avg * (1 + CORE_TAKE_PROFIT):
            for lot in held:
                sell(tag, lot, lot["qty"], "core +60%: everything sold")
            st["sold"] = {"price": px, "peak": px, "date": today.isoformat()}
            notify(f"CORE sold at {px:.2f} (+60%). Buying back when QQQ is 30% under its highest price since today.")
        elif week_id(today) not in st["deposit_weeks"]:
            buy(s, tag, cash * 0.99, "core weekly buy", today)
            st["deposit_weeks"].append(week_id(today))
    else:
        st["sold"]["peak"] = max(st["sold"]["peak"], px)
        trigger = st["sold"]["peak"] * (1 - CORE_BUY_BACK)
        log.info("CORE in cash since %s: QQQ %.2f, buys back at %.2f (-30%% from %.2f) | cash $%.2f",
                 st["sold"]["date"], px, trigger, st["sold"]["peak"], cash)
        if px <= trigger:
            buy(s, tag, cash * 0.99, f"core buy-back: QQQ 30% under {st['sold']['peak']:.2f}", today)
            st["sold"] = None
    st["closed"] += [l for l in st["lots"] if l["qty"] <= 1e-9]
    st["lots"] = [l for l in st["lots"] if l["qty"] > 1e-9]


def run_market(s: dict, tag: str, cash: float, now: dt.datetime) -> None:
    sym, kind, share, lookback = MARKETS[tag]
    st, rules = s[tag], Rules(dip_lookback=lookback)
    if kind == "stock":
        clock = api("GET", "/v2/clock")
        t_et = now.astimezone(ET)
        if not clock["is_open"] or not (dt.time(9, 40) <= t_et.time() <= dt.time(15, 50)):
            log.info("%s: market closed / outside 9:40-15:50 New York", tag)
            return
        today = t_et.date()
    else:
        today = now.date()
    if tag == "CORE":
        return run_core(s, tag, cash, today)
    closes = daily_closes(tag)
    m, dp = macd_state(closes, rules), in_dip(closes, rules)
    if PLAIN_MACD:
        dp = dp & False                                                # no dip buys
    up_now, up_before, dip_now, dip_before = bool(m.iloc[-1]), bool(m.iloc[-2]), bool(dp.iloc[-1]), bool(dp.iloc[-2])
    px = price(tag)
    high = closes.iloc[-lookback:].max()
    log.info("%s %s %.2f | MACD %s (before: %s) | %.1f%% under 3-month high%s | cash $%.2f | %d open buys",
             tag, sym, px, "UP" if up_now else "down", "UP" if up_before else "down",
             (1 - closes.iloc[-1] / high) * 100, " = DIP" if dip_now else "", cash, len(st["lots"]))

    for lot in ([] if PLAIN_MACD else st["lots"]):                     # profit rules, every run
        lot["peak"] = max(lot["peak"], px)
        act, _ = lot_action(lot["entry"], px, lot["peak"], lot["half"], rules)
        if act == "half":
            sell(tag, lot, lot["qty"] * rules.tp1_fraction, "+12%: sell half")
            lot["half"] = True
        elif act == "rest":
            sell(tag, lot, lot["qty"], "+25% reached" if px >= lot["entry"] * (1 + rules.tp2) else "back to +18%: profit locked")

    if st.get("signal_day") != today.isoformat():                     # daily signals, once per day
        st["signal_day"] = today.isoformat()
        week = week_id(today)
        if up_before and not up_now and any(l["qty"] > 0 for l in st["lots"]):
            for lot in st["lots"]:
                if lot["qty"] > 0:
                    sell(tag, lot, lot["qty"], "MACD sell signal: everything out")
        elif (up_now and not up_before) or (dip_now and not dip_before):
            buy(s, tag, cash * 0.99, "MACD buy signal" if up_now and not up_before else "12% dip under the 3-month high", today)
            st["deposit_weeks"].append(week)
        elif (up_now or dip_now) and week not in st["deposit_weeks"]:
            buy(s, tag, cash * 0.99, "weekly buy: new deposit + money from earlier sales" + (" (in a dip)" if dip_now else ""), today)
            st["deposit_weeks"].append(week)

    st["closed"] += [l for l in st["lots"] if l["qty"] <= 1e-9]
    st["lots"] = [l for l in st["lots"] if l["qty"] > 1e-9]


def run_once(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = load_state()
    cash = all_cash(eurusd(), now.date())
    for tag in MARKETS:
        try:
            run_market(s, tag, cash[tag], now)
        except Exception as e:                                         # one market failing must not stop the others
            log.exception("%s failed", tag)
            notify(f"ERROR in {tag}: {type(e).__name__}: {e}")
    save_state(s)
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_once()


if __name__ == "__main__":
    main()
