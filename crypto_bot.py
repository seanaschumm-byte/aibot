"""Aggressive BTC paper bot: up to 5 trades of EUR 100 each on BTC/USD (Alpaca paper).

Stops for good after 5 trades, or once total profit reaches +500% of the EUR 500 stake.
Run hourly (crypto trades 24/7). Orders carry the client_order_id prefix "btcbot-".

Usage: python crypto_bot.py run | report     (DRY_RUN=1 to not place orders)
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

from bot import api, eurusd, DATA_URL, DRY_RUN
from crypto_strategy import CParams, atr, entry_signal, trailing_stop
import journal
import market

SYMBOL = "BTC/USD"
POS_SYMBOL = "BTCUSD"
START = dt.datetime(2026, 11, 14, tzinfo=dt.timezone.utc)
MAX_TRADES = 5
EUR_PER_TRADE = 100.0
STOP_AT_PROFIT = 5.0          # +500% of total stake -> bot shuts down
PREFIX = "btcbot"
P = CParams()

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z {msg}"
    print(line, flush=True)
    LOG.append(line)


def hourly_bars(hours: int = 400) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    params = {"symbols": SYMBOL, "timeframe": "1Hour", "limit": 10000,
              "start": (now - dt.timedelta(hours=hours)).isoformat()}
    out = []
    while True:
        js = api("GET", "/v1beta3/crypto/us/bars", base=DATA_URL, params=params)
        out += (js.get("bars") or {}).get(SYMBOL, [])
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    # keep completed bars only
    return [b for b in out
            if dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")) + dt.timedelta(hours=1) <= now]


def my_orders() -> list[dict]:
    seen, out, after = set(), [], (START - dt.timedelta(days=1)).isoformat()
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


def ledger(orders: list[dict]) -> dict:
    """Trades = each buy until the position is sold again."""
    trades, cur, held = [], None, 0.0
    for o in orders:
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            continue
        px = float(o["filled_avg_price"])
        when = o.get("filled_at") or o["submitted_at"]
        if o["side"] == "buy":
            cur = {"opened": when, "entry": px, "qty": q, "cost": q * px, "proceeds": 0.0,
                   "cid": o["client_order_id"]}
            held += q
        else:
            held -= q
            if cur:
                cur["proceeds"] += q * px
                if held <= cur["qty"] * 0.01:   # sold (allow for fee dust)
                    cur["closed"] = when
                    cur["pnl"] = cur["proceeds"] - cur["cost"]
                    trades.append(cur)
                    cur, held = None, 0.0
    return {"closed": trades, "open": cur}


def position_qty() -> float:
    for p in api("GET", "/v2/positions"):
        if p["symbol"] == POS_SYMBOL:
            return float(p["qty"])
    return 0.0


def open_stops() -> list[dict]:
    return [o for o in api("GET", "/v2/orders", params={"status": "open"})
            if (o.get("client_order_id") or "").startswith(PREFIX + "-S")]


def order(side: str, cid: str, **kw) -> dict | None:
    body = {"symbol": SYMBOL, "side": side, "type": "market", "time_in_force": "gtc",
            "client_order_id": cid, **kw}
    log(f"ORDER {side} {kw} [{cid}]" + (" (dry run)" if DRY_RUN else ""))
    return None if DRY_RUN else api("POST", "/v2/orders", json=body)


def sell_all(why: str) -> None:
    for o in open_stops():
        if not DRY_RUN:
            api("DELETE", f"/v2/orders/{o['id']}")
    if not DRY_RUN:
        time.sleep(1)
    q = position_qty()
    log(f"EXIT: {why}")
    if q > 0:
        order("sell", f"{PREFIX}-X-{int(time.time())}", qty=f"{q:.9f}")


def set_stop(stop: float) -> None:
    """Server-side stop-limit so the downside is covered between hourly runs."""
    q = position_qty()
    stops = open_stops()
    limit = stop * 0.985
    if stops:
        s = stops[0]
        if float(s["stop_price"]) < stop - 1 and not DRY_RUN:
            api("PATCH", f"/v2/orders/{s['id']}",
                json={"stop_price": f"{stop:.2f}", "limit_price": f"{limit:.2f}"})
            log(f"trailing stop raised to {stop:.2f}")
    elif q > 0:
        order("sell", f"{PREFIX}-S-{int(time.time())}", type="stop_limit", qty=f"{q:.9f}",
              stop_price=f"{stop:.2f}", limit_price=f"{limit:.2f}")


def summary(led: dict, price: float, fx: float) -> tuple[str, float]:
    stake = MAX_TRADES * EUR_PER_TRADE * fx
    realized = sum(t["pnl"] for t in led["closed"])
    unreal = (price * led["open"]["qty"] - led["open"]["cost"]) if led["open"] else 0.0
    total = realized + unreal
    lines = [
        "## BTC risk bot (paper)",
        f"- Trades used: {len(led['closed']) + (1 if led['open'] else 0)} / {MAX_TRADES} "
        f"(EUR {EUR_PER_TRADE:.0f} each, EURUSD {fx:.4f})",
        f"- BTC price: ${price:,.2f}",
        f"- Realized P&L ${realized:+.2f}, open P&L ${unreal:+.2f}, "
        f"total ${total:+.2f} ({total / stake:+.1%} of ${stake:.2f} stake; bot stops at +{STOP_AT_PROFIT:.0%})",
        "", "| Opened | Entry | Exit value | P&L |", "|---|---|---|---|",
    ]
    for t in led["closed"]:
        lines.append(f"| {t['opened'][:16]} | {t['entry']:,.2f} | {t['proceeds']:.2f} | {t['pnl']:+.2f} |")
    if led["open"]:
        t = led["open"]
        lines.append(f"| {t['opened'][:16]} | {t['entry']:,.2f} | (open) | {unreal:+.2f} |")
    return "\n".join(lines), total / stake


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    fx = eurusd()
    bars = hourly_bars()
    price = bars[-1]["c"] if bars else 0.0
    led = ledger(my_orders())
    text, ret = summary(led, price, fx)

    if mode == "run":
        m = market.mood(api, DATA_URL)
        log(market.summary_line(m))
        journal.record_closes("btc", [
            {"cid": t.get("cid"), "entry": t["entry"], "pnl_usd": round(t["pnl"], 4),
             "pnl_pct": round(t["pnl"] / t["cost"], 4), "opened": t["opened"], "closed": t.get("closed")}
            for t in led["closed"] if t.get("cid")])
        a = atr(bars, P.atr_n)
        if led["open"]:
            t = led["open"]
            gain = price / t["entry"] - 1
            opened = dt.datetime.fromisoformat(t["opened"].replace("Z", "+00:00"))
            since = [b for b in bars
                     if dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")) >= opened]
            cur = max([float(s["stop_price"]) for s in open_stops()] or [0.0])
            stop = trailing_stop(since, t["entry"], cur, a, P)
            log(f"IN TRADE entry {t['entry']:,.2f} now {price:,.2f} ({gain:+.1%}), stop {stop:,.2f}")
            if gain >= P.take_profit:
                sell_all(f"+{gain:.0%} on this trade (500% cap)")
            elif ret >= STOP_AT_PROFIT:
                sell_all("total profit reached +500%: shutting down")
            elif price <= stop:
                sell_all(f"price {price:,.2f} <= stop {stop:,.2f}")
            else:
                set_stop(stop)
        elif ret >= STOP_AT_PROFIT:
            log("total profit reached +500%: bot finished")
        elif len(led["closed"]) >= MAX_TRADES:
            log(f"all {MAX_TRADES} trades used: bot finished")
        else:
            sig, why = entry_signal(bars, P)
            log(f"scan: {why}")
            if sig:
                notional = EUR_PER_TRADE * fx
                log(f"BREAKOUT above {sig['breakout_level']:,.2f}: buy ${notional:.2f} BTC, "
                    f"stop {sig['stop']:,.2f}")
                bcid = f"{PREFIX}-B-{int(time.time())}"
                o = order("buy", bcid, notional=f"{notional:.2f}")
                if o:
                    journal.write("btc", "entry", cid=bcid, usd=round(notional, 2), signal=sig, mood=m)
                if o:
                    for _ in range(20):
                        o = api("GET", f"/v2/orders/{o['id']}")
                        if o["status"] == "filled":
                            break
                        time.sleep(1)
                    set_stop(sig["stop"])
        text, _ = summary(ledger(my_orders()), price, fx)

    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("```\n" + "\n".join(LOG) + "\n```\n\n" + text + "\n")


if __name__ == "__main__":
    main()
