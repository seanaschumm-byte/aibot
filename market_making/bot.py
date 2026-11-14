"""Market Making bot (Alpaca paper, crypto): keeps one bid and one ask per coin in the book,
re-quoting around the mid price; profits when both sides fill (buy low, sell high = the spread).

  python -m market_making.bot           one cycle (GitHub Actions every 5 minutes)
  python -m market_making.bot --loop    continuous, every 30 s (PC / VPS: much closer to real MM)

Virtual budget $500 (its own ledger from orders with client_order_id prefix "mm-").
Safety: max $250 inventory per coin, kill switch at -$50 (10%) total P&L: then it only sells.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time

import requests

from .quoting import MMParams, needs_requote, quotes, sigma_from_closes

log = logging.getLogger("market_making")
TRADE, DATA = "https://paper-api.alpaca.markets", "https://data.alpaca.markets"
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}
COINS = ["BTC/USD", "ETH/USD"]
BUDGET = 500.0
KILL_SWITCH = -50.0
START = "2026-11-14T00:00:00Z"
PREFIX = "mm"
P = MMParams()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")
FEE_COIN = 0.0015      # maker fee taken from the coins received on a filled buy (approximation)


def api(method: str, path: str, base: str = TRADE, **kw):
    for i in range(4):
        r = requests.request(method, base + path, headers=H, timeout=20, **kw)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 * (i + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else None
    r.raise_for_status()


def notify(text: str) -> None:
    log.info("ALERT %s", text)
    hook = os.getenv("DISCORD_WEBHOOK_URL")
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    try:
        if hook:
            requests.post(hook, json={"content": f"[Market Making] {text}"[:1900]}, timeout=10)
        if token and chat:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"[Market Making] {text}"}, timeout=10)
    except requests.RequestException:
        pass


# ---------------------------------------------------------------- ledger
def my_orders() -> list[dict]:
    out, seen, after = [], set(), START
    while True:
        batch = api("GET", "/v2/orders", params={"status": "all", "after": after, "direction": "asc", "limit": 500})
        new = [o for o in batch if o["id"] not in seen]
        for o in new:
            seen.add(o["id"])
            if (o.get("client_order_id") or "").startswith(PREFIX + "-"):
                out.append(o)
        if len(batch) < 500 or not new:
            return out
        after = batch[-1]["submitted_at"]


def ledger(orders: list[dict]) -> dict:
    """Cash, inventory, average cost, realised spread P&L and fees, per coin."""
    cash, inv, cost, realized, fees, round_trips = BUDGET, {}, {}, 0.0, 0.0, 0
    for o in orders:
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            continue
        s, px = o["symbol"], float(o["filled_avg_price"])
        if o["side"] == "buy":
            cash -= q * px
            got = q * (1 - FEE_COIN)
            fees += q * px * FEE_COIN
            inv[s] = inv.get(s, 0) + got
            cost[s] = cost.get(s, 0) + q * px
        else:
            proceeds = q * px * (1 - FEE_COIN)
            fees += q * px * FEE_COIN
            cash += proceeds
            avg = cost.get(s, 0) / inv[s] if inv.get(s) else px
            realized += proceeds - avg * q
            cost[s] = cost.get(s, 0) - avg * q
            inv[s] = inv.get(s, 0) - q
            round_trips += 1
    return {"cash": cash, "inv": {k: v for k, v in inv.items() if v > 1e-9}, "cost": cost,
            "realized": realized, "fees": fees, "round_trips": round_trips}


# ---------------------------------------------------------------- market data
def book(coin: str) -> tuple[float, float]:
    q = api("GET", "/v1beta3/crypto/us/latest/quotes", base=DATA, params={"symbols": coin})["quotes"][coin]
    return float(q["bp"]), float(q["ap"])


def sigma_1m(coin: str) -> float:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=90)).isoformat()
    bars = api("GET", "/v1beta3/crypto/us/bars", base=DATA,
               params={"symbols": coin, "timeframe": "1Min", "start": start, "limit": 200})["bars"].get(coin, [])
    return sigma_from_closes([b["c"] for b in bars])


# ---------------------------------------------------------------- one cycle
def open_quotes() -> dict[tuple[str, str], dict]:
    out = {}
    for o in api("GET", "/v2/orders", params={"status": "open", "limit": 500}):
        if (o.get("client_order_id") or "").startswith(PREFIX + "-"):
            out[(o["symbol"], o["side"])] = o
    return out


def place(coin: str, side: str, qty: float, price: float) -> None:
    px = f"{price:.2f}"
    api("POST", "/v2/orders", json={"symbol": coin, "side": side, "type": "limit", "time_in_force": "gtc",
                                   "qty": f"{qty:.9f}", "limit_price": px,
                                   "client_order_id": f"{PREFIX}-{side[0].upper()}-{coin.split('/')[0]}-{time.time_ns()}"})


def run_once() -> dict:
    led = ledger(my_orders())
    live = open_quotes()
    mids, pnl_open = {}, 0.0
    for c in COINS:
        bid, ask = book(c)
        mids[c] = (bid, ask, (bid + ask) / 2)
        if c in led["inv"]:
            pnl_open += led["inv"][c] * mids[c][2] - led["cost"].get(c, 0)
    total_pnl = led["realized"] + pnl_open
    killed = total_pnl <= KILL_SWITCH
    log.info("cash $%.2f | realised $%.2f (fees $%.2f, %d sells) | open P&L $%.2f | total $%+.2f%s",
             led["cash"], led["realized"], led["fees"], led["round_trips"], pnl_open, total_pnl,
             " | KILL SWITCH: only selling" if killed else "")
    state = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "total_pnl": round(total_pnl, 2),
             "realized": round(led["realized"], 2), "fees": round(led["fees"], 2), "coins": {}}
    cash_left = led["cash"] - sum(float(o["qty"]) * float(o["limit_price"]) for (s, side), o in live.items() if side == "buy")
    for c in COINS:
        best_bid, best_ask, mid = mids[c]
        inv_q = led["inv"].get(c, 0.0)
        inv_usd = inv_q * mid
        sig = sigma_1m(c)
        qt = quotes(mid, best_bid, best_ask, sig, inv_usd, max(cash_left, 0), P)
        if killed:
            qt["quote_bid"] = False
        log.info("%s mid %.2f spread %.3f%% sigma1m %.3f%% inv $%.2f -> bid %.2f%s / ask %.2f%s", c, mid,
                 (best_ask - best_bid) / mid * 100, sig * 100, inv_usd, qt["bid"], "" if qt["quote_bid"] else " (off)",
                 qt["ask"], "" if qt["quote_ask"] else " (off)")
        for side, want, price in (("buy", qt["quote_bid"], qt["bid"]), ("sell", qt["quote_ask"], qt["ask"])):
            cur = live.get((c, side))
            cur_px = float(cur["limit_price"]) if cur else None
            if cur and (not want or needs_requote(cur_px, price, P)):
                api("DELETE", f"/v2/orders/{cur['id']}")
                cur = None
                time.sleep(0.5)
            if want and not cur:
                qty = (P.order_usd / price) if side == "buy" else min(inv_q, P.order_usd / price * 1.5)
                if qty * price >= 10:
                    place(c, side, int(qty * 1e9) / 1e9, price)
                    if side == "buy":
                        cash_left -= P.order_usd
        state["coins"][c] = {"mid": mid, "bid": round(qt["bid"], 2), "ask": round(qt["ask"], 2),
                             "half_spread_pct": round(qt["half_spread"] * 100, 3), "inventory_usd": round(inv_usd, 2)}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)
    if killed:
        notify(f"Kill switch: total P&L ${total_pnl:.2f}. No new buy quotes; only selling inventory.")
    return state


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    while True:
        try:
            run_once()
        except Exception as e:
            log.exception("cycle failed")
            notify(f"ERROR: {e}")
            if "--loop" not in sys.argv:
                raise
        if "--loop" not in sys.argv:
            return
        time.sleep(30)


if __name__ == "__main__":
    main()
