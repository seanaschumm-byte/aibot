"""Simulate the market-making bot on real 1-minute Alpaca crypto bars (last 30 days).

Fill model (deliberately a bit conservative): a resting bid fills only if a later minute trades
strictly BELOW it, an ask only if strictly ABOVE it (being first in the queue at your exact price
isn't guaranteed). Maker fee 0.15% per fill. Quotes are refreshed every N minutes:
5 = GitHub Actions cadence, 1 = a PC/VPS loop.
  python -m market_making.backtest
"""
from __future__ import annotations

import datetime as dt
import os

import requests

from .quoting import MMParams, quotes, sigma_from_closes

DATA = "https://data.alpaca.markets"
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}


def minute_bars(coin: str, days: int = 30) -> list[dict]:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    params, out = {"symbols": coin, "timeframe": "1Min", "start": start, "limit": 10000}, []
    while True:
        js = requests.get(DATA + "/v1beta3/crypto/us/bars", headers=H, params=params, timeout=30).json()
        out += js.get("bars", {}).get(coin, [])
        if not js.get("next_page_token"):
            return out
        params["page_token"] = js["next_page_token"]


def simulate(bars: list[dict], every: int = 5, p: MMParams = MMParams(), budget: float = 250.0) -> dict:
    cash, inv, cost, fees, realized, buys, sells, max_inv = budget, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0
    bid = ask = None
    closes: list[float] = []
    for i, b in enumerate(bars):
        # fills against the quotes that were resting during this minute
        if bid is not None and b["l"] < bid and cash >= p.order_usd:
            q = p.order_usd / bid
            cash -= p.order_usd
            fees += p.order_usd * p.maker_fee
            inv += q * (1 - p.maker_fee)
            cost += p.order_usd
            buys += 1
            bid = None
        if ask is not None and b["h"] > ask and inv * ask >= 10:
            q = min(inv, p.order_usd / ask * 1.5)
            got = q * ask * (1 - p.maker_fee)
            fees += q * ask * p.maker_fee
            avg = cost / inv
            realized += got - avg * q
            cost -= avg * q
            inv -= q
            cash += got
            sells += 1
            ask = None
        closes.append(b["c"])
        max_inv = max(max_inv, inv * b["c"])
        if i % every == 0 and len(closes) > 30:
            mid = b["c"]
            sig = sigma_from_closes(closes[-90:])
            qt = quotes(mid, mid * 0.99995, mid * 1.00005, sig, inv * mid, cash, p)
            bid = qt["bid"] if qt["quote_bid"] else None
            ask = qt["ask"] if qt["quote_ask"] else None
    last = bars[-1]["c"]
    final = cash + inv * last
    hodl = budget / bars[0]["c"] * last
    return {"final": final, "pnl": final - budget, "realized": realized, "fees": fees, "buys": buys,
            "sells": sells, "inventory_usd": inv * last, "max_inventory": max_inv,
            "price_change": last / bars[0]["c"] - 1, "hold_pnl": hodl - budget}


def main() -> None:
    lines = ["## Market Making simulation: last 30 days of 1-minute bars, $250 per coin, maker fee 0.15%", "",
             "| Coin | Re-quote | Buys / sells filled | Fees | Realised spread P&L | Inventory left | **Total P&L** | Just holding the coin |",
             "|---|---|---|---|---|---|---|---|"]
    for coin in ("BTC/USD", "ETH/USD"):
        bars = minute_bars(coin)
        for every in (5, 1):
            r = simulate(bars, every)
            lines.append(f"| {coin} | every {every} min | {r['buys']} / {r['sells']} | ${r['fees']:.2f} | "
                         f"${r['realized']:+.2f} | ${r['inventory_usd']:.2f} | **${r['pnl']:+.2f}** | "
                         f"${r['hold_pnl']:+.2f} ({r['price_change']:+.1%}) |")
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
