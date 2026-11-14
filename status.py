"""Status page: one section per bot, built from the Alpaca paper account.

All bots share one Alpaca account; each bot's orders carry its own client_order_id prefix, so
this script splits the account per bot. Writes STATUS.md (refreshed hourly by GitHub Actions).
  python status.py
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import requests

API = "https://paper-api.alpaca.markets"
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}
NL = ZoneInfo("Europe/Amsterdam")
CRYPTO_FEE = 0.0025

BOTS = [  # prefix, name, what it does
    ("aibot", "Bot 1 - Stocks VWAP dip (stopped)", "US stocks intraday, EUR 75/week budget"),
    ("btcbot", "Bot 2 - BTC risk (stopped)", "BTC breakouts, 5 trades of EUR 100"),
    ("dip25", "Bot 3 - Crypto daily dip (stopped)", "EUR 25/day on dips in an uptrend, sells at +20%"),
    ("dip10", "Bot 4 - Weekly dip (stopped)", "EUR 175/week grid -10..-30% under weekly average, sells at +12%"),
    ("rsv", "$ Bot 5 - Smart Reserve", "EUR 500 + EUR 100/week pot, EUR 25 per dip"),
    ("sl", "Sean Lavarge", "2x crypto ETFs (BITU/ETHT/SOLZ, SBIT/ETHD), EUR 500 + EUR 150/week"),
    ("sr", "Sean Realistisch", "trend following: SPY/QQQ (2x SSO/QLD), GLD, TLT, IBIT; $500 + $300/month"),
    ("mm", "Market Making", "bid + ask quotes on BTC/ETH around the mid price, $500 budget, profits from the spread"),
    ("manual25", "Manual trades", "5 x EUR 25 crypto: sell at +30%, stop-loss at -40%"),
]


def get(path: str, **params):
    r = requests.get(API + path, headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def all_orders(since: str = "2026-11-13T00:00:00Z") -> list[dict]:
    out, seen, after = [], set(), since
    while True:
        batch = get("/v2/orders", status="all", after=after, direction="asc", limit=500)
        new = [o for o in batch if o["id"] not in seen]
        for o in new:
            seen.add(o["id"])
        out += new
        if len(batch) < 500 or not new:
            return out
        after = batch[-1]["submitted_at"]


def bot_of(cid: str) -> str | None:
    cid = cid or ""
    for prefix, *_ in BOTS:
        if cid.startswith(prefix + "-"):
            return prefix
    return None


def summarize(orders: list[dict], prices: dict[str, float]) -> dict:
    """Per bot: money in, money out, open holdings at current prices, P&L."""
    res = {p: {"bought": 0.0, "sold": 0.0, "qty": {}, "trades": 0, "open_orders": 0, "last": None, "fills": []}
           for p, *_ in BOTS}
    for o in orders:
        b = bot_of(o.get("client_order_id"))
        if not b:
            continue
        r = res[b]
        if o["status"] in ("new", "accepted", "pending_new", "partially_filled", "held"):
            r["open_orders"] += 1
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            continue
        px, sym = float(o["filled_avg_price"]), o["symbol"]
        crypto = "/" in sym
        if o["side"] == "buy":
            r["bought"] += q * px
            r["qty"][sym] = r["qty"].get(sym, 0) + q * ((1 - CRYPTO_FEE) if crypto else 1)
            r["trades"] += 1
        else:
            r["sold"] += q * px * ((1 - CRYPTO_FEE) if crypto else 1)
            r["qty"][sym] = r["qty"].get(sym, 0) - q
        r["last"] = o.get("filled_at") or o["submitted_at"]
        r["fills"].append((r["last"], o["side"], sym, q, px))
    for r in res.values():
        r["qty"] = {s: q for s, q in r["qty"].items() if q > 1e-6}
        r["open_value"] = sum(q * prices.get(s.replace("/", ""), 0) for s, q in r["qty"].items())
        r["pnl"] = r["sold"] + r["open_value"] - r["bought"]
    return res


def fmt(p: float) -> str:
    return f"{p:,.2f}" if p >= 1 else f"{p:.6f}"


def render(res: dict, account: dict, prices: dict) -> str:
    now = dt.datetime.now(NL)
    lines = [f"# Bot status: {now:%d-%m-%Y %H:%M} (Dutch time)",
             "", f"Alpaca paper account: equity **${float(account['equity']):,.2f}**, "
                 f"cash ${float(account['cash']):,.2f}. All bots share this account; below it is split per bot.",
             "", "| Bot | Bought | Sold | Holding now | Profit / loss | Buys | Open orders | Last fill |",
             "|---|---|---|---|---|---|---|---|"]
    for prefix, name, _ in BOTS:
        r = res[prefix]
        pct = f" ({r['pnl'] / r['bought']:+.1%})" if r["bought"] else ""
        last = dt.datetime.fromisoformat(r["last"].replace("Z", "+00:00")).astimezone(NL).strftime("%d-%m %H:%M") if r["last"] else "-"
        lines.append(f"| {name} | ${r['bought']:,.2f} | ${r['sold']:,.2f} | ${r['open_value']:,.2f} | "
                     f"{'🟢' if r['pnl'] > 0.005 else '🔴' if r['pnl'] < -0.005 else '⚪'} ${r['pnl']:+,.2f}{pct} | "
                     f"{r['trades']} | {r['open_orders']} | {last} |")
    lines += ["", "Profit / loss = sold + current value of holdings − bought (after Alpaca's 0.25% crypto fee). "
                  "Money a bot hasn't spent yet isn't counted."]
    for prefix, name, what in BOTS:
        r = res[prefix]
        lines += ["", f"## {name}", f"_{what}_ · order label `{prefix}-`", ""]
        if not r["trades"]:
            lines.append("No trades yet." + (f" {r['open_orders']} order(s) waiting." if r["open_orders"] else ""))
            continue
        if r["qty"]:
            lines += ["| Holding | Quantity | Price now | Value |", "|---|---|---|---|"]
            for s, q in r["qty"].items():
                p = prices.get(s.replace("/", ""), 0)
                lines.append(f"| {s} | {q:.6g} | ${fmt(p)} | ${q * p:,.2f} |")
        else:
            lines.append("No open positions (everything sold).")
        lines += ["", "Last fills:", ""]
        for t, side, s, q, px in r["fills"][-5:][::-1]:
            when = dt.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(NL).strftime("%d-%m %H:%M")
            lines.append(f"- {when}: {'BUY' if side == 'buy' else 'SELL'} {q:.6g} {s} @ ${fmt(px)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    account = get("/v2/account")
    prices = {p["symbol"]: float(p["current_price"]) for p in get("/v2/positions")}
    text = render(summarize(all_orders(), prices), account, prices)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "STATUS.md"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
