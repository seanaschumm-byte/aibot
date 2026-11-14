"""Stop-loss guard for the manual trades (client_order_id prefix "manual25-").

Each manual buy has a +30% limit sell. Alpaca crypto has no OCO, so a second (stop) order on the
same coins would be refused. This script runs every 15 minutes: when a coin falls to -40% of its
buy price, it cancels the +30% order and sells the coins at market.
  python manual_guard.py            (DRY_RUN=1 to only report)
"""
from __future__ import annotations

import os
import time

import requests

API = "https://paper-api.alpaca.markets"
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}
PREFIX = "manual25"
STOP_LOSS = -0.40
DRY = os.getenv("DRY_RUN") == "1"


def call(method: str, path: str, **kw):
    r = requests.request(method, API + path, headers=H, timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.text else None


def plan(orders: list[dict], prices: dict[str, float]) -> list[dict]:
    """Which open manual +30% sells must be replaced by a stop-loss sale?"""
    buys = {o["symbol"]: float(o["filled_avg_price"]) for o in orders
            if o["client_order_id"].startswith(PREFIX + "-B") and o.get("filled_avg_price")}
    out = []
    for o in orders:
        if not o["client_order_id"].startswith(PREFIX + "-S") or o["status"] not in ("new", "accepted", "partially_filled"):
            continue
        sym, buy = o["symbol"], buys.get(o["symbol"])
        price = prices.get(sym.replace("/", ""))
        if not buy or not price:
            continue
        stop = buy * (1 + STOP_LOSS)
        out.append({"order": o, "symbol": sym, "buy": buy, "price": price, "stop": stop,
                    "change": price / buy - 1, "hit": price <= stop})
    return out


def main() -> None:
    orders = call("GET", "/v2/orders", params={"status": "all", "after": "2026-11-13T00:00:00Z", "limit": 500})
    prices = {p["symbol"]: float(p["current_price"]) for p in call("GET", "/v2/positions")}
    for x in plan(orders, prices):
        print(f"{x['symbol']:9} buy {x['buy']:.6g} now {x['price']:.6g} ({x['change']:+.1%}) "
              f"stop {x['stop']:.6g}{'  -> STOP-LOSS' if x['hit'] else ''}")
        if not x["hit"] or DRY:
            continue
        o = x["order"]
        call("DELETE", f"/v2/orders/{o['id']}")
        time.sleep(1)
        tag = x["symbol"].split("/")[0]
        r = call("POST", "/v2/orders", json={"symbol": x["symbol"], "side": "sell", "type": "market",
                                             "time_in_force": "gtc", "qty": o["qty"],
                                             "client_order_id": f"{PREFIX}-X-{tag}-{int(time.time())}"})
        print(f"  sold {o['qty']} {x['symbol']} at market (stop-loss -40%): order {r.get('status')}")


if __name__ == "__main__":
    main()
