"""AI trading bot Shakur (Alpaca paper): 6 strategies side by side, each with 1/6 of the money.
See config.py for the sleeves and why they were chosen.

Every sleeve has its own cash (its share of EUR 500 + EUR 100/week, plus its own results) and its
own positions; orders carry client_order_id "sk-<SLEEVE>-<B|S>-<SYMBOL>-<ms>". Signals use completed
daily candles: stock sleeves act once per trading day after 9:45 New York, crypto sleeves once per
UTC day. Runs every 30 minutes (GitHub Actions).
  python -m shakur.bot            (DRY_RUN=1 to only log)
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

import journal
from common import DATA_URL, DRY_RUN, api, eurusd
from justin.strategies import ALL_STRATEGIES, indicators
from .config import CRYPTO, CRYPTO_FEE, PREFIX, SHARE, SLEEVES, START, START_EUR, STOCKS, WEEKLY_EUR

log = logging.getLogger("shakur")
ET = ZoneInfo("America/New_York")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")


def notify(text: str) -> None:
    log.info("ALERT %s", text)
    hook, token, chat = os.getenv("DISCORD_WEBHOOK_URL"), os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    try:
        if hook:
            requests.post(hook, json={"content": f"[Shakur] {text}"}, timeout=10)
        if token and chat:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"[Shakur] {text}"}, timeout=10)
    except requests.RequestException:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    for tag in SLEEVES:
        s.setdefault(tag, {"positions": {}, "closed": [], "last_day": None, "topped_up": []})
    return s


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def deposits_usd(today: dt.date, fx: float) -> float:
    if today < START:
        return 0.0
    return (START_EUR + WEEKLY_EUR * ((today - START).days // 7)) * fx * SHARE


def ledgers(fx: float, today: dt.date) -> dict[str, tuple[float, dict[str, float]]]:
    """(cash, holdings) per sleeve, from Shakur's own orders."""
    res = {tag: [deposits_usd(today, fx), {}] for tag in SLEEVES}
    after = dt.datetime.combine(START - dt.timedelta(days=1), dt.time(), dt.timezone.utc).isoformat()
    for o in api("GET", "/v2/orders", params={"status": "all", "after": after, "limit": 500, "direction": "asc"}):
        cid = o.get("client_order_id") or ""
        parts = cid.split("-")
        if not cid.startswith(PREFIX + "-") or len(parts) < 3 or parts[1] not in res:
            continue
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            continue
        px, crypto = float(o["filled_avg_price"]), "/" in o["symbol"]
        led = res[parts[1]]
        if o["side"] == "buy":
            led[0] -= q * px
            led[1][o["symbol"]] = led[1].get(o["symbol"], 0) + q * ((1 - CRYPTO_FEE) if crypto else 1)
        else:
            led[0] += q * px * ((1 - CRYPTO_FEE) if crypto else 1)
            led[1][o["symbol"]] = led[1].get(o["symbol"], 0) - q
    return {t: (c, {s: q for s, q in h.items() if q > 1e-9}) for t, (c, h) in res.items()}


def daily_bars(symbols: list[str], crypto: bool) -> dict[str, pd.DataFrame]:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=420)).isoformat()
    path = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "limit": 10000}
    if not crypto:
        params.update({"feed": "iex", "adjustment": "all"})
    raw: dict[str, list] = {}
    while True:
        js = api("GET", path, base=DATA_URL, params=params)
        for s, b in (js.get("bars") or {}).items():
            raw.setdefault(s, []).extend(b)
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    out = {}
    for s, b in raw.items():
        df = pd.DataFrame(b)
        df["t"] = pd.to_datetime(df["t"], utc=True)
        today = dt.datetime.now(dt.timezone.utc).date() if crypto else dt.datetime.now(ET).date()
        day = df["t"].dt.date if crypto else df["t"].dt.tz_convert(ET).dt.date
        df = df[day < today]                                              # completed days only
        out[s] = indicators(df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low",
                                                               "c": "close", "v": "volume"}))
    return out


def order(tag: str, symbol: str, side: str, *, usd: float | None = None, qty: float | None = None) -> dict:
    crypto = "/" in symbol
    body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "gtc" if crypto else "day",
            "client_order_id": f"{PREFIX}-{tag}-{side[0].upper()}-{symbol.replace('/', '')}-{int(time.time() * 1000)}"}
    body.update({"notional": f"{usd:.2f}"} if usd else {"qty": f"{int(qty * 1e9) / 1e9:.9f}" if crypto else f"{int(qty * 1e6) / 1e6:.6f}"})
    log.info("ORDER %s %s %s %s%s", tag, side, symbol, body.get("notional") or body.get("qty"), " (dry run)" if DRY_RUN else "")
    if DRY_RUN:
        return {"filled_qty": qty or 0, "filled_avg_price": 0, "status": "dry"}
    o = api("POST", "/v2/orders", json=body)
    for _ in range(30):
        o = api("GET", f"/v2/orders/{o['id']}")
        if o["status"] in ("filled", "canceled", "rejected", "expired"):
            break
        time.sleep(1)
    return o


def run_sleeve(s: dict, tag: str, cash: float, held: dict[str, float], rows: dict[str, pd.Series], today: dt.date) -> None:
    name, universe, max_pos, add = SLEEVES[tag]
    st, strat = s[tag], ALL_STRATEGIES[name]
    crypto = "/" in universe[0]
    min_order = 10.0 if crypto else 1.0
    rows = {sym: r for sym, r in rows.items() if sym in universe}
    for sym, q in held.items():                                           # exits
        p = st["positions"].setdefault(sym, {"entry": float(rows[sym]["close"]) if sym in rows else 0, "days": 0,
                                             "opened": today.isoformat()})
        p["days"] += 1
        if sym in rows and strat["exit"](rows[sym], p):
            o = order(tag, sym, "sell", qty=q)
            px = float(o.get("filled_avg_price") or rows[sym]["close"])
            res = px / p["entry"] - 1 if p["entry"] else 0.0
            st["closed"].append({"symbol": sym, "entry": p["entry"], "exit": px, "result": round(res, 4),
                                 "opened": p["opened"], "closed": today.isoformat()})
            st["positions"].pop(sym)
            cash += q * px * ((1 - CRYPTO_FEE) if crypto else 1)
            journal.write("shakur", "close", cid=f"{tag}-{sym}-{p['opened']}", sleeve=tag, symbol=sym, pnl_pct=round(res, 4))
            notify(f"{tag}: SELL {sym} @ {px:,.2f} ({res:+.1%}) [{name}]")
    open_now = [x for x in held if x in st["positions"]]
    equity = cash + sum(held[x] * float(rows[x]["close"]) for x in open_now if x in rows)
    week = f"{today.isocalendar()[0]}W{today.isocalendar()[1]:02d}"
    if add and open_now and cash >= min_order and week not in st["topped_up"]:   # trend sleeves: add new money
        sym = open_now[0]
        o = order(tag, sym, "buy", usd=cash * 0.99)
        if float(o.get("filled_qty") or 0) > 0:
            st["topped_up"].append(week)
            notify(f"{tag}: weekly top-up ${cash * 0.99:.2f} {sym} [{name}]")
        return
    cands = sorted((strat["rank"](r), sym) for sym, r in rows.items() if sym not in open_now and strat["entry"](r))
    log.info("%s [%s]: cash $%.2f, equity $%.2f, open %s, signals %s", tag, name, cash, equity, open_now or "-",
             [c[1] for c in cands] or "-")
    for _, sym in cands[:max(max_pos - len(open_now), 0)]:              # entries
        usd = (cash if add else min(cash, equity / max_pos)) * 0.99
        if usd < min_order:
            break
        o = order(tag, sym, "buy", usd=usd)
        q, px = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
        if q > 0:
            st["positions"][sym] = {"entry": px, "days": 0, "opened": today.isoformat()}
            cash -= q * px
            journal.write("shakur", "entry", cid=f"{tag}-{sym}-{today}", sleeve=tag, symbol=sym, price=px, usd=round(usd, 2))
            notify(f"{tag}: BUY {sym} ${usd:.2f} @ {px:,.2f} [{name}]")


def run_once(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = load_state()
    clock = api("GET", "/v2/clock")
    t_et = now.astimezone(ET)
    stock_day = t_et.date() if clock["is_open"] and dt.time(9, 45) <= t_et.time() <= dt.time(15, 50) else None
    crypto_day = now.date()
    due = {t for t, (n, u, m, a) in SLEEVES.items()
           if s[t]["last_day"] != (crypto_day if "/" in u[0] else stock_day).__str__()
           and ("/" in u[0] or stock_day is not None)}
    if not due:
        log.info("nothing due (stocks: %s, crypto already done today)", "open" if stock_day else "closed")
        return s
    fx = eurusd()
    led = ledgers(fx, now.date())
    rows: dict[str, pd.Series] = {}
    if any("/" not in SLEEVES[t][1][0] for t in due):
        rows.update({k: d.iloc[-1] for k, d in daily_bars(STOCKS, False).items() if len(d) > 200})
    if any("/" in SLEEVES[t][1][0] for t in due):
        rows.update({k: d.iloc[-1] for k, d in daily_bars(CRYPTO, True).items() if len(d) > 200})
    for tag in sorted(due):
        day = crypto_day if "/" in SLEEVES[tag][1][0] else stock_day
        try:
            run_sleeve(s, tag, led[tag][0], led[tag][1], rows, day)
            s[tag]["last_day"] = str(day)
        except Exception as e:                                            # one sleeve failing must not stop the rest
            log.exception("%s failed", tag)
            notify(f"ERROR in {tag}: {type(e).__name__}: {e}")
    save_state(s)
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_once()


if __name__ == "__main__":
    main()
