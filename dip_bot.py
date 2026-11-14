"""Daily crypto dip bot: at most one EUR 25 buy per day (UTC), only when a coin shows a
'dip in an uptrend' setup. Every buy gets its own sell order at +20% (GTC, 24/7).

Runs hourly. Orders carry the client_order_id prefix "dip25-".
Usage: python dip_bot.py run | report      (DRY_RUN=1 to not place orders)
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

from bot import api, eurusd, DATA_URL, DRY_RUN
import journal
import market
from dip_strategy import DipParams, dip_signal

COINS = ["ETH/USD", "SOL/USD", "DOGE/USD", "XRP/USD", "AVAX/USD", "LINK/USD"]  # not BTC: btc bot
START = dt.date(2026, 11, 14)
END = dt.date(2026, 12, 14)          # one month of daily buys
EUR_PER_DAY = 25.0
TAKE_PROFIT = 0.20
FEE_KEEP = 0.9975                    # Alpaca takes 0.25% of the bought coins as fee
PREFIX = "dip25"
P = DipParams()
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}Z {msg}"
    print(line, flush=True)
    LOG.append(line)


def hourly_bars(hours: int = 300) -> dict[str, list[dict]]:
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
    for sym in out:   # completed bars only
        out[sym] = [b for b in out[sym] if dt.datetime.fromisoformat(
            b["t"].replace("Z", "+00:00")) + dt.timedelta(hours=1) <= now]
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


def lots(orders: list[dict]) -> list[dict]:
    """One lot per buy: dip25-B-<COIN>-<ts> paired with its sell dip25-S-<COIN>-<ts>."""
    sells = {o["client_order_id"].replace("-S-", "-B-", 1): o
             for o in orders if o["client_order_id"].startswith(PREFIX + "-S-")}
    out = []
    for o in orders:
        if not o["client_order_id"].startswith(PREFIX + "-B-"):
            continue
        q = float(o.get("filled_qty") or 0)
        out.append({"cid": o["client_order_id"], "symbol": o["symbol"], "status": o["status"],
                    "day": (o.get("submitted_at") or "")[:10], "qty": q,
                    "price": float(o["filled_avg_price"]) if q else 0.0,
                    "sell": sells.get(o["client_order_id"])})
    return out


def place_take_profit(lot: dict) -> None:
    qty = int(lot["qty"] * FEE_KEEP * 1e9) / 1e9
    target = lot["price"] * (1 + TAKE_PROFIT)
    price = f"{target:.2f}" if target >= 1 else f"{target:.6f}"
    cid = lot["cid"].replace("-B-", "-S-", 1)
    log(f"SELL ORDER {lot['symbol']} {qty} @ {price} (+{TAKE_PROFIT:.0%})" + (" (dry run)" if DRY_RUN else ""))
    if not DRY_RUN:
        api("POST", "/v2/orders", json={"symbol": lot["symbol"], "side": "sell", "type": "limit",
                                       "time_in_force": "gtc", "qty": f"{qty:.9f}",
                                       "limit_price": price, "client_order_id": cid})


def report(all_lots: list[dict], fx: float) -> str:
    filled = [l for l in all_lots if l["qty"] > 0]
    invested = sum(l["qty"] * l["price"] for l in filled)
    sold = [l for l in filled if l["sell"] and l["sell"]["status"] == "filled"]
    profit = sum(float(l["sell"]["filled_qty"]) * float(l["sell"]["filled_avg_price"])
                 - l["qty"] * l["price"] for l in sold)
    lines = ["## Crypto dip bot (EUR 25/day, sell at +20%)",
             f"- Period {START} to {END}; buys: {len(filled)} (${invested:.2f} invested, EURUSD {fx:.4f})",
             f"- Sold at +20%: {len(sold)} lots, realized profit ${profit:+.2f}",
             f"- Still open: {len(filled) - len(sold)} lots waiting for +20%",
             "", "| Day | Coin | Bought @ | Sell @ | Status |", "|---|---|---|---|---|"]
    for l in filled:
        s = l["sell"]
        lines.append(f"| {l['day']} | {l['symbol']} | {l['price']:,.4f} | "
                     f"{(s or {}).get('limit_price') or '-'} | {(s or {}).get('status', 'no sell order')} |")
    return "\n".join(lines)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    fx = eurusd()
    all_lots = lots(my_orders())
    today = dt.datetime.now(dt.timezone.utc).date()

    if mode == "run":
        m = market.mood(api, DATA_URL)
        log(market.summary_line(m))
        journal.daily_market("dip25", m)
        journal.record_closes("dip25", [
            {"cid": l["cid"], "symbol": l["symbol"], "buy": l["price"],
             "sell": float(l["sell"]["filled_avg_price"]),
             "pnl_usd": round(float(l["sell"]["filled_qty"]) * float(l["sell"]["filled_avg_price"])
                              * 0.9975 - l["qty"] * l["price"], 4),
             "closed": l["sell"].get("filled_at")}
            for l in all_lots if l["qty"] > 0 and l["sell"] and l["sell"]["status"] == "filled"])
        # safety net: every filled buy must have its +20% sell order
        for l in all_lots:
            if l["qty"] > 0 and not l["sell"]:
                place_take_profit(l)

        if not (START <= today <= END):
            log("outside the buying period: only managing sell orders")
        elif any(l["day"] == today.isoformat() and l["status"] not in ("canceled", "rejected", "expired")
                 for l in all_lots):
            log("already bought today: next buy tomorrow")
        else:
            bars = hourly_bars()
            signals = []
            for c in COINS:
                sig, why = dip_signal(bars.get(c, []), P)
                log(f"  {c:9} {why}")
                if sig:
                    signals.append((c, sig))
            if signals:
                coin, sig = max(signals, key=lambda x: x[1]["change_24h"])   # strongest coin wins
                factor = market.crypto_size_factor(m)
                notional = EUR_PER_DAY * fx * factor
                if factor < 1:
                    log(f"market is greedy ({m.get('fear_greed_label')}): buying {factor:.0%} of EUR 25")
                cid = f"{PREFIX}-B-{coin.split('/')[0]}-{int(time.time())}"
                log(f"BUY {coin} ${notional:.2f}: dip to VWAP {sig['vwap']:.4f}, "
                    f"low {sig['dip_low']:.4f}, 24h {sig['change_24h']:+.1%}" + (" (dry run)" if DRY_RUN else ""))
                if not DRY_RUN:
                    o = api("POST", "/v2/orders", json={"symbol": coin, "side": "buy", "type": "market",
                                                       "time_in_force": "gtc", "notional": f"{notional:.2f}",
                                                       "client_order_id": cid})
                    for _ in range(20):
                        o = api("GET", f"/v2/orders/{o['id']}")
                        if o["status"] == "filled":
                            break
                        time.sleep(1)
                    journal.write("dip25", "entry", cid=cid, symbol=coin, usd=round(notional, 2),
                                  signal=sig, size_factor=factor, mood=m,
                                  others={c: s["change_24h"] for c, s in signals})
                    if o["status"] == "filled":
                        place_take_profit({"cid": cid, "symbol": coin, "qty": float(o["filled_qty"]),
                                           "price": float(o["filled_avg_price"])})
            else:
                log("no dip setup right now: checking again next hour")
        all_lots = lots(my_orders())

    text = report(all_lots, fx)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write("```\n" + "\n".join(LOG) + "\n```\n\n" + text + "\n")


if __name__ == "__main__":
    main()
