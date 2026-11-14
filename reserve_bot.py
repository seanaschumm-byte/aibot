"""Smart Reserve DCA bot (Alpaca paper, crypto vs USD).

Euro pot: EUR 500 start + EUR 100 every Monday (ISO week) from START. Buys EUR 25 per dip
signal (see reserve_strategy.py), max one buy per run, best (lowest RSI) coin first.
Sells 100% of a coin with a trailing take-profit; the money goes back to the pot.
Runs every 15 minutes. Orders carry the client_order_id prefix "rsv-".
Usage: python reserve_bot.py run | report      (DRY_RUN=1 to not place orders)
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

from common import api, eurusd, DATA_URL, DRY_RUN
import journal
import market
from reserve_strategy import CAPS, Rules, check_buy, check_sell

COINS = list(CAPS)
START = dt.date(2026, 11, 14)
START_EUR = 500.0
WEEKLY_EUR = 100.0
ORDER_EUR = 25.0
FEE = 0.0025          # Alpaca crypto market-order fee
PREFIX = "rsv"
R = Rules()
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z {msg}"
    print(line, flush=True)
    LOG.append(line)


def deposits_eur(today: dt.date) -> float:
    if today < START:
        return 0.0
    first_monday = START - dt.timedelta(days=START.weekday())
    weeks_after = (today - first_monday).days // 7
    return START_EUR + WEEKLY_EUR * weeks_after


def bars(timeframe: str, hours: float) -> dict[str, list[dict]]:
    now = dt.datetime.now(dt.timezone.utc)
    params = {"symbols": ",".join(COINS), "timeframe": timeframe, "limit": 10000,
              "start": (now - dt.timedelta(hours=hours)).isoformat()}
    out: dict[str, list[dict]] = {c: [] for c in COINS}
    while True:
        js = api("GET", "/v1beta3/crypto/us/bars", base=DATA_URL, params=params)
        for sym, bs in (js.get("bars") or {}).items():
            out[sym].extend(bs)
        if not js.get("next_page_token"):
            return out
        params["page_token"] = js["next_page_token"]


def my_orders() -> list[dict]:
    seen, out = set(), []
    after = dt.datetime.combine(START - dt.timedelta(days=1), dt.time(), dt.timezone.utc).isoformat()
    while True:
        batch = api("GET", "/v2/orders", params={"status": "all", "after": after,
                                                 "direction": "asc", "limit": 500})
        new = [o for o in batch if o["id"] not in seen]
        for o in new:
            seen.add(o["id"])
            if (o.get("client_order_id") or "").startswith(PREFIX + "-"):
                out.append(o)
        if len(batch) < 500 or not new:
            return out
        after = batch[-1]["submitted_at"]


def ledger(orders: list[dict], today: dt.date, fx: float) -> dict:
    """Cash and per-coin holdings of this bot only."""
    dep_eur = deposits_eur(today)
    cash = dep_eur * fx
    coins = {c: {"qty": 0.0, "cost": 0.0, "last_buy_price": None, "last_buy_time": None,
                 "first_buy_time": None, "last_buy_cid": None} for c in COINS}
    closes = []
    for o in orders:
        q = float(o.get("filled_qty") or 0)
        if q <= 0 or o["symbol"] not in coins:
            continue
        px = float(o["filled_avg_price"])
        when = o.get("filled_at") or o["submitted_at"]
        c = coins[o["symbol"]]
        if o["side"] == "buy":
            cash -= q * px
            c["qty"] += q * (1 - FEE)
            c["cost"] += q * px
            c["last_buy_price"], c["last_buy_time"], c["last_buy_cid"] = px, when, o["client_order_id"]
            c["first_buy_time"] = c["first_buy_time"] or when
        else:
            got = q * px * (1 - FEE)
            cash += got
            share = min(1.0, q / c["qty"]) if c["qty"] else 1.0
            cost_part = c["cost"] * share
            c["qty"] -= q
            c["cost"] -= cost_part
            if c["qty"] <= 1e-9 or share >= 0.999:
                closes.append({"cid": o["client_order_id"], "symbol": o["symbol"],
                               "pnl_usd": round(got - cost_part, 4),
                               "pnl_pct": round(got / cost_part - 1, 4) if cost_part else None,
                               "closed": when})
                coins[o["symbol"]] = {"qty": 0.0, "cost": 0.0, "last_buy_price": None,
                                      "last_buy_time": None, "first_buy_time": None,
                                      "last_buy_cid": None}
    return {"deposits_eur": dep_eur, "cash": cash, "coins": coins, "closes": closes}


def hours_since(iso: str | None, now: dt.datetime) -> float | None:
    if not iso:
        return None
    return (now - dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600


def market_sell(coin: str, qty: float, why: str) -> None:
    avail = qty
    for p in api("GET", "/v2/positions"):
        if p["symbol"] == coin.replace("/", ""):
            avail = min(qty, float(p.get("qty_available") or p["qty"]))
    q = int(avail * 1e9) / 1e9
    log(f"SELL {coin} {q} ({why})" + (" (dry run)" if DRY_RUN else ""))
    if not DRY_RUN and q > 0:
        api("POST", "/v2/orders", json={"symbol": coin, "side": "sell", "type": "market",
                                       "time_in_force": "gtc", "qty": f"{q:.9f}",
                                       "client_order_id": f"{PREFIX}-S-{coin.split('/')[0]}-{int(time.time())}"})


def report(led: dict, prices: dict, fx: float) -> str:
    held = {c: v for c, v in led["coins"].items() if v["qty"] > 0}
    value = sum(v["qty"] * prices.get(c, 0) for c, v in held.items())
    pot = led["cash"] + value
    dep_usd = led["deposits_eur"] * fx
    realized = sum(x["pnl_usd"] for x in led["closes"])
    lines = ["## Smart Reserve bot (EUR 500 + EUR 100/week, EUR 25 per dip)",
             f"- Deposited: EUR {led['deposits_eur']:.0f} (${dep_usd:.2f} at EURUSD {fx:.4f})",
             f"- Pot value: ${pot:.2f} (EUR {pot / fx:.2f}) = cash ${led['cash']:.2f} + coins ${value:.2f}",
             f"- Result: ${pot - dep_usd:+.2f} ({(pot / dep_usd - 1) if dep_usd else 0:+.2%}); "
             f"closed trades {len(led['closes'])}, realized ${realized:+.2f}",
             "", "| Coin | Qty | Avg cost | Now | P&L | % of pot (max) |", "|---|---|---|---|---|---|"]
    for c, v in held.items():
        avg = v["cost"] / v["qty"]
        p = prices.get(c, 0)
        lines.append(f"| {c} | {v['qty']:.6g} | {avg:.4g} | {p:.4g} | {p / avg - 1:+.1%} | "
                     f"{v['qty'] * p / pot:.0%} ({CAPS[c]:.0%}) |")
    return "\n".join(lines)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    fx = eurusd()
    now = dt.datetime.now(dt.timezone.utc)
    led = ledger(my_orders(), now.date(), fx)
    h1, h4, m15 = bars("1Hour", 24 * 9), bars("4Hour", 24 * 25), bars("15Min", 6)
    prices = {c: (m15[c] or h1[c] or [{"c": 0}])[-1]["c"] for c in COINS}

    if mode == "run" and now.date() >= START:
        m = market.mood(api, DATA_URL)
        log(market.summary_line(m))
        journal.record_closes("reserve", led["closes"])
        # 1. sells: trailing take-profit per coin
        sold = False
        for c, v in led["coins"].items():
            if v["qty"] <= 0:
                continue
            avg = v["cost"] / v["qty"]
            since = [b for b in h1[c] if v["last_buy_time"] and b["t"] >= v["last_buy_time"][:13]]
            since += [b for b in m15[c] if v["last_buy_time"] and b["t"] >= v["last_buy_time"]]
            ok, why = check_sell(c, avg, prices[c], since, R)
            log(f"  HOLD {c:9} avg {avg:.4g} now {prices[c]:.4g}: {why}")
            if ok:
                market_sell(c, v["qty"], why)
                sold = True
        # 2. buys: at most one per run, lowest RSI first
        pot = led["cash"] + sum(v["qty"] * prices[c] for c, v in led["coins"].items())
        order_usd = ORDER_EUR * fx
        if sold:
            log("sold this run: next buy check in 15 minutes")
        elif led["cash"] < order_usd:
            log(f"pot cash ${led['cash']:.2f} < EUR 25: waiting for next deposit or a sale")
        else:
            cands = []
            for c in COINS:
                v = led["coins"][c]
                ok, why, rs = check_buy(c, prices[c], h1[c], h4[c], m15[c], v["qty"] * prices[c], pot,
                                        order_usd, hours_since(v["last_buy_time"], now),
                                        v["last_buy_price"], R)
                log(f"  SCAN {c:9} RSI {rs:4.0f}: {why}")
                if ok:
                    cands.append((rs, c))
            if cands:
                rs, c = min(cands)
                cid = f"{PREFIX}-B-{c.split('/')[0]}-{int(time.time())}"
                log(f"BUY {c} ${order_usd:.2f} (EUR {ORDER_EUR:.0f}), RSI {rs:.0f}"
                    + (" (dry run)" if DRY_RUN else ""))
                if not DRY_RUN:
                    api("POST", "/v2/orders", json={"symbol": c, "side": "buy", "type": "market",
                                                   "time_in_force": "gtc", "notional": f"{order_usd:.2f}",
                                                   "client_order_id": cid})
                high7 = max(b["h"] for b in h1[c][-168:])
                journal.write("reserve", "entry", cid=cid, symbol=c, usd=round(order_usd, 2),
                              price=prices[c], rsi=round(rs, 1), below_7d_high=round(prices[c] / high7 - 1, 4),
                              pot_usd=round(pot, 2), coin_share=round(led["coins"][c]["qty"] * prices[c] / pot, 4),
                              mood=m, candidates=[x[1] for x in cands])
            else:
                log("no dip right now: money stays in the pot")
        led = ledger(my_orders(), now.date(), fx)

    text = report(led, prices, fx)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("```\n" + "\n".join(LOG) + "\n```\n\n" + text + "\n")


if __name__ == "__main__":
    main()
