"""Weekly crypto dip bot: EUR 175 per week, limit buys at -10/-15/-20/-30% of last week's
average price (7-day VWAP), each fill sold at +12%.

Every Monday (ISO week) it cancels last week's unfilled buys, records the new weekly
average per coin, and places a fresh grid with all available money. Runs hourly to put a
+12% sell order on every new fill. Orders carry the client_order_id prefix "dip10-".
Usage: python weekly_dip_bot.py run | report      (DRY_RUN=1 to not place orders)
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

from bot import api, eurusd, DATA_URL, DRY_RUN
import journal
import market
from weekly_dip_strategy import (TAKE_PROFIT, crashed, fmt_price, level_price, plan_orders,
                                 weekly_sigma, weekly_vwap, LEVELS)

COINS = ["ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]  # not BTC: btc bot
START = dt.date(2026, 11, 14)
END = dt.date(2026, 12, 13)          # 5 weekly deposits (ISO weeks 46-50)
EUR_PER_WEEK = 175.0
FEE_KEEP = 0.9975                    # Alpaca keeps 0.25% of bought coins as fee
PREFIX = "dip10"
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z {msg}"
    print(line, flush=True)
    LOG.append(line)


def week_id(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}W{w:02d}"


def hourly_bars(hours: int = 750) -> dict[str, list[dict]]:
    now = dt.datetime.now(dt.timezone.utc)
    params = {"symbols": ",".join(COINS), "timeframe": "1Hour", "limit": 10000,
              "start": (now - dt.timedelta(hours=hours)).isoformat()}
    out: dict[str, list[dict]] = {c: [] for c in COINS}
    while True:
        js = api("GET", "/v1beta3/crypto/us/bars", base=DATA_URL, params=params)
        for sym, bars in (js.get("bars") or {}).items():
            out[sym].extend(bars)
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    return out


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


def deposits(today: dt.date, fx: float) -> tuple[int, float]:
    if today < START:
        return 0, 0.0
    first = START - dt.timedelta(days=START.weekday())          # Monday of the start week
    last = min(today, END)
    n = (last - first).days // 7 + 1
    return n, n * EUR_PER_WEEK * fx


def ledger(orders: list[dict], today: dt.date, fx: float) -> dict:
    n, dep = deposits(today, fx)
    cash, lots = dep, []
    sells = {o["client_order_id"].replace("-S-", "-B-", 1): o
             for o in orders if o["client_order_id"].startswith(PREFIX + "-S-")}
    for o in orders:
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            continue
        px = float(o["filled_avg_price"])
        if o["side"] == "buy":
            cash -= q * px
            lots.append({"cid": o["client_order_id"], "symbol": o["symbol"], "qty": q, "price": px,
                         "day": (o.get("filled_at") or o["submitted_at"])[:10],
                         "sell": sells.get(o["client_order_id"]),
                         "done": o["status"] not in ("new", "partially_filled", "accepted",
                                                     "pending_new")})
        else:
            cash += q * px
    return {"weeks": n, "deposits": dep, "cash": cash, "lots": lots}


def place_sell(lot: dict) -> None:
    qty = int(lot["qty"] * FEE_KEEP * 1e9) / 1e9
    price = fmt_price(lot["price"] * (1 + TAKE_PROFIT))
    log(f"SELL {lot['symbol']} {qty} @ {price} (+{TAKE_PROFIT:.0%})" + (" (dry run)" if DRY_RUN else ""))
    if not DRY_RUN:
        api("POST", "/v2/orders", json={"symbol": lot["symbol"], "side": "sell", "type": "limit",
                                       "time_in_force": "gtc", "qty": f"{qty:.9f}", "limit_price": price,
                                       "client_order_id": lot["cid"].replace("-B-", "-S-", 1)})


def new_week(orders: list[dict], today: dt.date, fx: float, m: dict) -> None:
    wk = week_id(today)
    # 1. cancel last week's unfilled buys (their money returns to the budget)
    for o in orders:
        if o["client_order_id"].startswith(PREFIX + "-B-") and \
                o["status"] in ("new", "accepted", "pending_new", "partially_filled"):
            log(f"cancel old buy {o['client_order_id']}")
            if not DRY_RUN:
                api("DELETE", f"/v2/orders/{o['id']}")
    if not DRY_RUN:
        time.sleep(2)
        orders = my_orders()
    led = ledger(orders, today, fx)
    log(f"WEEK {wk}: deposits {led['weeks']} x EUR {EUR_PER_WEEK:.0f} = ${led['deposits']:.2f}, "
        f"available ${led['cash']:.2f}")

    # 2. this week's average market price per coin
    bars = hourly_bars()
    avgs, sigmas = {}, {}
    for c in COINS:
        b = bars.get(c, [])
        if len(b) < 168:
            log(f"  {c:9} not enough data")
            continue
        avg, last, sig = weekly_vwap(b), b[-1]["c"], weekly_sigma(b)
        avgs[c], sigmas[c] = avg, sig
        flag = " CRASH (>35% down in 30 days): buying anyway" if crashed(b) else ""
        log(f"  {c:9}{flag} weekly avg {fmt_price(avg)}, now {fmt_price(last)} ({last / avg - 1:+.1%}), "
            f"volatility {sig / avg:.1%}; buys at "
            + ", ".join(f"{fmt_price(level_price(avg, sig, i))} ({level_price(avg, sig, i) / avg - 1:.0%})"
                        for i in range(len(LEVELS))))

    # 3. place the grid
    use, weights = market.weekly_plan(m)
    log(f"market mood {m.get('fear_greed_label')}: using {use:.0%} of available cash, "
        f"level weights {weights}")
    plan = plan_orders(avgs, led["cash"] * 0.995 * use, sigma_by_coin=sigmas, weights=weights)
    journal.write("weekly_dip", "week", week=wk, cash=round(led["cash"], 2), use=use, weights=weights,
                  mood=m, coins={c: {"avg": avgs[c], "sigma_pct": sigmas[c] / avgs[c],
                                     "levels": [level_price(avgs[c], sigmas[c], i) for i in range(len(LEVELS))]}
                                 for c in avgs})
    for p in plan:
        tag = p["coin"].split("/")[0]
        cid = f"{PREFIX}-B-{tag}-{wk}-L{p['level']}-{int(time.time())}"
        log(f"BUY LIMIT {p['coin']} {p['qty']:.6f} @ {fmt_price(p['limit'])} (${p['usd']:.2f}, "
            f"{p['dist']:.0%})" + (" (dry run)" if DRY_RUN else ""))
        if not DRY_RUN:
            try:
                api("POST", "/v2/orders", json={"symbol": p["coin"], "side": "buy", "type": "limit",
                                               "time_in_force": "gtc", "qty": f"{p['qty']:.9f}",
                                               "limit_price": fmt_price(p["limit"]),
                                               "client_order_id": cid})
            except RuntimeError as e:
                log(f"  order refused: {e}")


def report(led: dict, fx: float) -> str:
    lots = led["lots"]
    sold = [l for l in lots if l["sell"] and l["sell"]["status"] == "filled"]
    profit = sum(float(l["sell"]["filled_qty"]) * float(l["sell"]["filled_avg_price"])
                 - l["qty"] * l["price"] for l in sold)
    invested_open = sum(l["qty"] * l["price"] for l in lots if l not in sold)
    lines = ["## Weekly dip bot (EUR 175/week, buy -10/-15/-20/-30% of weekly avg, sell +12%)",
             f"- Deposits: {led['weeks']} x EUR {EUR_PER_WEEK:.0f} = ${led['deposits']:.2f} (EURUSD {fx:.4f})",
             f"- Free cash (incl. money reserved in open buy orders): ${led['cash']:.2f}",
             f"- Lots bought: {len(lots)}; sold at +12%: {len(sold)} (profit ${profit:+.2f}); "
             f"still open: {len(lots) - len(sold)} (${invested_open:.2f} at cost)",
             "", "| Day | Coin | Bought @ | Sell @ | Status |", "|---|---|---|---|---|"]
    for l in lots:
        s = l["sell"] or {}
        lines.append(f"| {l['day']} | {l['symbol']} | {fmt_price(l['price'])} | "
                     f"{s.get('limit_price', '-')} | {s.get('status', 'waiting')} |")
    return "\n".join(lines)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    fx = eurusd()
    today = dt.datetime.now(dt.timezone.utc).date()
    orders = my_orders()

    if mode == "run":
        m = market.mood(api, DATA_URL)
        log(market.summary_line(m))
        led = ledger(orders, today, fx)
        for lot in led["lots"]:                    # every fill gets its +12% sell
            if not lot["sell"] and lot["done"]:
                place_sell(lot)
            if lot["done"] and not journal.has("weekly_dip", "entry", "cid", lot["cid"]):
                level = lot["cid"].split("-")[4] if lot["cid"].count("-") >= 5 else "?"
                journal.write("weekly_dip", "entry", cid=lot["cid"], symbol=lot["symbol"],
                              price=lot["price"], usd=round(lot["qty"] * lot["price"], 2),
                              level=level, mood=m)
        journal.record_closes("weekly_dip", [
            {"cid": l["cid"], "symbol": l["symbol"], "buy": l["price"],
             "sell": float(l["sell"]["filled_avg_price"]),
             "pnl_usd": round(float(l["sell"]["filled_qty"]) * float(l["sell"]["filled_avg_price"])
                              * 0.9975 - l["qty"] * l["price"], 4),
             "closed": l["sell"].get("filled_at")}
            for l in led["lots"] if l["sell"] and l["sell"]["status"] == "filled"])
        wk = week_id(today)
        has_week = any(f"-{wk}-" in o["client_order_id"] for o in orders
                       if o["client_order_id"].startswith(PREFIX + "-B-")
                       and (o["status"] not in ("canceled", "rejected", "expired")
                            or float(o.get("filled_qty") or 0) > 0))
        if START <= today <= END and not has_week:
            new_week(orders, today, fx, m)
        elif today > END and not has_week:
            for o in orders:   # test period over: stop buying, keep the +12% sells
                if o["client_order_id"].startswith(PREFIX + "-B-") and o["status"] in ("new", "accepted"):
                    log(f"period over, cancel {o['client_order_id']}")
                    if not DRY_RUN:
                        api("DELETE", f"/v2/orders/{o['id']}")
        else:
            log(f"week {wk} grid already placed; waiting for dips")
        orders = my_orders()

    text = report(ledger(orders, today, fx), fx)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("```\n" + "\n".join(LOG) + "\n```\n\n" + text + "\n")


if __name__ == "__main__":
    main()
