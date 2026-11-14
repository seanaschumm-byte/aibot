"""Alpaca paper-trading bot: VWAP pullback, run once per invocation (GitHub Actions every 5 min).

Usage:
  python bot.py run      # manage open trade, look for a new entry
  python bot.py report   # performance summary since START_DATE
  python bot.py status   # account / ledger snapshot, no trading

Env: ALPACA_API_KEY, ALPACA_SECRET_KEY, optional DRY_RUN=1, EURUSD (override FX rate).

The bot keeps a *virtual* budget: EUR 75 deposited every Monday from START_DATE.
It only ever spends that budget, never the full paper balance. All its orders carry
the client_order_id prefix "aibot-", so the ledger is rebuilt from Alpaca each run
(no local state needed).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

from strategy import Params, evaluate, market_ok, day_change, position_size

# ------------------------------------------------------------------ config
START_DATE = dt.date(2026, 11, 16)      # first Monday
END_DATE = dt.date(2026, 12, 16)        # one month of trading
WEEKLY_DEPOSIT_EUR = 75.0
EURUSD_FALLBACK = 1.15                  # used if the live FX lookup fails

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META", "AMZN", "GOOGL",
            "NFLX", "AVGO", "PLTR", "SOFI", "INTC", "F", "QQQ"]
MARKET = "SPY"

RISK_PER_TRADE = 0.01          # Brandt: ~1% of capital at risk per trade
DAILY_LOSS_LIMIT = 0.02        # Cameron: hard daily max loss, then stop for the day
MAX_TRADES_PER_DAY = 3
BREAKEVEN_AT_R = 1.0           # Minervini: don't let a good gain become a loss
NO_ENTRY_BEFORE = dt.time(9, 45)   # let the opening chaos settle
NO_ENTRY_AFTER = dt.time(15, 0)
FLATTEN_AT = dt.time(15, 50)       # intraday only: flat before the close
PREFIX = "aibot"

ET = ZoneInfo("America/New_York")
TRADE_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
DRY_RUN = os.environ.get("DRY_RUN") == "1"

S = requests.Session()
S.headers.update({
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", "proxy-injected"),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "proxy-injected"),
})

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"{dt.datetime.now(ET):%H:%M:%S} {msg}"
    print(line, flush=True)
    LOG.append(line)


# ------------------------------------------------------------------ http
def api(method: str, path: str, base: str = TRADE_URL, **kw):
    for attempt in range(3):
        r = S.request(method, base + path, timeout=20, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else None
    r.raise_for_status()


def get_bars(symbols: list[str], timeframe: str, start: dt.datetime,
             end: dt.datetime | None = None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    params = {"symbols": ",".join(symbols), "timeframe": timeframe, "feed": "iex",
              "start": start.isoformat(), "limit": 10000, "adjustment": "raw"}
    if end:
        params["end"] = end.isoformat()
    while True:
        js = api("GET", "/v2/stocks/bars", base=DATA_URL, params=params)
        for sym, bars in (js.get("bars") or {}).items():
            out[sym].extend(bars)
        if not js.get("next_page_token"):
            return out
        params["page_token"] = js["next_page_token"]


def latest_price(symbol: str) -> float:
    js = api("GET", f"/v2/stocks/{symbol}/trades/latest", base=DATA_URL, params={"feed": "iex"})
    return float(js["trade"]["p"])


def eurusd() -> float:
    if os.environ.get("EURUSD"):
        return float(os.environ["EURUSD"])
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=EUR&to=USD", timeout=10)
        return float(r.json()["rates"]["USD"])
    except Exception:
        return EURUSD_FALLBACK


# ------------------------------------------------------------------ ledger
def bot_orders() -> list[dict]:
    """Every order this bot placed since START_DATE, oldest first."""
    seen, out = set(), []
    after = dt.datetime.combine(START_DATE - dt.timedelta(days=3), dt.time(), ET).isoformat()
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
    """Number of Monday deposits made so far and their USD total."""
    if today < START_DATE:
        return 0, 0.0
    n = (min(today, END_DATE) - START_DATE).days // 7 + 1
    return n, n * WEEKLY_DEPOSIT_EUR * fx


def ledger(orders: list[dict], today: dt.date, fx: float) -> dict:
    n_dep, dep = deposits(today, fx)
    cash, holdings, trades, open_trade = dep, {}, [], {}
    for o in orders:
        q = float(o.get("filled_qty") or 0)
        if q <= 0:
            continue
        px = float(o["filled_avg_price"])
        sym = o["symbol"]
        filled = o.get("filled_at") or o["submitted_at"]
        if o["side"] == "buy":
            cash -= q * px
            holdings[sym] = holdings.get(sym, 0) + q
            t = open_trade.setdefault(sym, {"symbol": sym, "opened": filled, "cost": 0.0,
                                            "proceeds": 0.0, "qty": 0.0})
            t["cost"] += q * px
            t["qty"] += q
            t["buy_cid"] = o["client_order_id"]
        else:
            cash += q * px
            holdings[sym] = holdings.get(sym, 0) - q
            if sym in open_trade:
                open_trade[sym]["proceeds"] += q * px
            if holdings[sym] <= 1e-6:
                holdings.pop(sym)
                if sym in open_trade:
                    t = open_trade.pop(sym)
                    t["closed"] = filled
                    t["pnl"] = t["proceeds"] - t["cost"]
                    trades.append(t)
    return {"deposits_n": n_dep, "deposits_usd": dep, "cash": cash, "holdings": holdings,
            "closed_trades": trades, "open_trades": open_trade}


def parse_buy_cid(cid: str) -> tuple[float, float]:
    """aibot-B-SYM-epoch-stopcents-targetcents -> (stop, target)."""
    parts = cid.split("-")
    return int(parts[-2]) / 100, int(parts[-1]) / 100


# ------------------------------------------------------------------ orders
def submit(symbol: str, side: str, qty: float, cid: str, **extra) -> dict | None:
    body = {"symbol": symbol, "side": side, "qty": f"{qty:.4f}", "time_in_force": "day",
            "type": "market", "client_order_id": cid[:128], **extra}
    log(f"ORDER {side} {qty:.4f} {symbol} {extra or ''} [{cid}]" + (" (dry run)" if DRY_RUN else ""))
    if DRY_RUN:
        return None
    return api("POST", "/v2/orders", json=body)


def wait_fill(order_id: str, secs: int = 20) -> dict:
    o = {}
    for _ in range(secs):
        o = api("GET", f"/v2/orders/{order_id}")
        if o["status"] in ("filled", "canceled", "rejected", "expired"):
            break
        time.sleep(1)
    return o


def open_stop_orders(symbol: str) -> list[dict]:
    return [o for o in api("GET", "/v2/orders", params={"status": "open", "symbols": symbol})
            if (o.get("client_order_id") or "").startswith(PREFIX + "-")]


def exit_position(symbol: str, qty: float, why: str) -> None:
    for o in open_stop_orders(symbol):
        if not DRY_RUN:
            api("DELETE", f"/v2/orders/{o['id']}")
    if not DRY_RUN:
        time.sleep(1)
    log(f"EXIT {symbol}: {why}")
    submit(symbol, "sell", qty, f"{PREFIX}-X-{symbol}-{int(time.time())}")


def place_stop(symbol: str, qty: float, stop: float) -> None:
    try:
        submit(symbol, "sell", qty, f"{PREFIX}-S-{symbol}-{int(time.time())}",
               type="stop", stop_price=f"{stop:.2f}")
    except RuntimeError as e:  # e.g. broker rejects fractional stops: bot watches the stop itself
        log(f"server-side stop not accepted, bot will manage stop: {e}")


# ------------------------------------------------------------------ main loop
def manage(led: dict, now: dt.datetime) -> None:
    positions = {p["symbol"]: p for p in api("GET", "/v2/positions")}
    for sym, held in list(led["holdings"].items()):
        qty = min(held, float(positions.get(sym, {}).get("qty", 0)))
        if qty <= 0:
            continue
        t = led["open_trades"].get(sym, {})
        entry = t["cost"] / t["qty"] if t.get("qty") else float(positions[sym]["avg_entry_price"])
        stop, target = parse_buy_cid(t.get("buy_cid", f"x-0-{int(entry*98)}-{int(entry*104)}"))
        price = latest_price(sym)
        log(f"HOLD {sym} {qty:.4f} @ {entry:.2f} now {price:.2f} stop {stop:.2f} target {target:.2f}")

        if now.time() >= FLATTEN_AT or now.date() > END_DATE:
            exit_position(sym, qty, "end-of-day flatten")
        elif price >= target:
            exit_position(sym, qty, f"target hit {price:.2f} >= {target:.2f}")
        elif price <= stop:
            exit_position(sym, qty, f"stop hit {price:.2f} <= {stop:.2f}")
        else:
            stops = open_stop_orders(sym)
            if not stops:
                place_stop(sym, qty, stop)
            elif price >= entry + BREAKEVEN_AT_R * (entry - stop):
                for o in stops:
                    if float(o["stop_price"]) < entry and not DRY_RUN:
                        api("PATCH", f"/v2/orders/{o['id']}", json={"stop_price": f"{entry:.2f}"})
                        log(f"stop moved to breakeven {entry:.2f}")


def look_for_entry(led: dict, now: dt.datetime, equity: float) -> None:
    today = now.date()
    if led["holdings"]:
        return log("already in a trade")
    if not (NO_ENTRY_BEFORE <= now.time() < NO_ENTRY_AFTER):
        return log("outside entry window")
    if today > END_DATE:
        return log("test month is over: no new trades")
    todays = [t for t in led["closed_trades"] if t["closed"][:10] == today.isoformat()]
    if len(todays) >= MAX_TRADES_PER_DAY:
        return log("max trades for today reached")
    day_pnl = sum(t["pnl"] for t in todays)
    if day_pnl <= -DAILY_LOSS_LIMIT * equity:
        return log(f"daily loss limit hit ({day_pnl:.2f}): done for today")

    session_open = dt.datetime.combine(today, dt.time(9, 30), ET)
    bars = get_bars(UNIVERSE + [MARKET], "5Min", session_open)
    cutoff = now - dt.timedelta(minutes=5)
    for sym in bars:  # drop the still-forming bar
        bars[sym] = [b for b in bars[sym]
                     if dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")) <= cutoff]

    ok, why = market_ok(bars[MARKET])
    log(f"market filter: {'OK' if ok else 'NO'} ({why})")
    if not ok:
        return

    daily = get_bars(UNIVERSE, "1Day", session_open - dt.timedelta(days=40), session_open)
    minutes = (now - session_open).total_seconds() / 60
    spy_chg = day_change(bars[MARKET])
    signals = []
    for sym in UNIVERSE:
        hist = daily.get(sym, [])[-20:]
        if not hist or not bars.get(sym):
            continue
        avg_vol = sum(b["v"] for b in hist) / len(hist)
        sig, reason = evaluate(sym, bars[sym], avg_vol, minutes, spy_chg, Params())
        log(f"  {sym:5} {reason}")
        if sig:
            signals.append(sig)
    if not signals:
        return
    sig = max(signals, key=lambda s: s.rel_strength)
    qty = position_size(equity, led["cash"], sig.entry, sig.stop, RISK_PER_TRADE)
    if qty * sig.entry < 1:
        return log(f"position too small (${qty * sig.entry:.2f}) for {sig.symbol}")
    log(f"BUY SIGNAL {sig.symbol} entry~{sig.entry} stop {sig.stop} target {sig.target} "
        f"({', '.join(sig.notes)})")
    cid = f"{PREFIX}-B-{sig.symbol}-{int(time.time())}-{round(sig.stop*100)}-{round(sig.target*100)}"
    o = submit(sig.symbol, "buy", qty, cid)
    if o:
        o = wait_fill(o["id"])
        filled = float(o.get("filled_qty") or 0)
        if filled > 0:
            log(f"filled {filled} @ {o['filled_avg_price']}")
            place_stop(sig.symbol, filled, sig.stop)


def snapshot(fx: float) -> tuple[dict, float]:
    today = dt.datetime.now(ET).date()
    led = ledger(bot_orders(), today, fx)
    equity = led["cash"]
    for sym, q in led["holdings"].items():
        try:
            equity += q * latest_price(sym)
        except Exception:
            pass
    return led, equity


def report(led: dict, equity: float, fx: float) -> str:
    trades = led["closed_trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    pnl = sum(t["pnl"] for t in trades)
    lines = [
        "## AI bot: VWAP pullback (paper)",
        f"- Period: {START_DATE} to {END_DATE}",
        f"- Deposits: {led['deposits_n']} x EUR {WEEKLY_DEPOSIT_EUR:.0f} = ${led['deposits_usd']:.2f} (EURUSD {fx:.4f})",
        f"- Equity: ${equity:.2f}  (cash ${led['cash']:.2f})",
        f"- Gain vs deposits: ${equity - led['deposits_usd']:+.2f}"
        + (f" ({(equity / led['deposits_usd'] - 1):+.2%})" if led["deposits_usd"] else ""),
        f"- Closed trades: {len(trades)}, wins {len(wins)}"
        + (f" ({len(wins) / len(trades):.0%})" if trades else "") + f", realized P&L ${pnl:+.2f}",
        "", "| Opened | Symbol | Cost | Proceeds | P&L |", "|---|---|---|---|---|",
    ]
    for t in trades:
        lines.append(f"| {t['opened'][:16]} | {t['symbol']} | {t['cost']:.2f} | "
                     f"{t['proceeds']:.2f} | {t['pnl']:+.2f} |")
    return "\n".join(lines)


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    fx = eurusd()
    led, equity = snapshot(fx)

    if mode in ("report", "status"):
        text = report(led, equity, fx)
        print(text)
        write_summary(text)
        return

    clock = api("GET", "/v2/clock")
    now = dt.datetime.now(ET)
    if not clock["is_open"]:
        log(f"market closed (next open {clock['next_open']})")
        return
    if now.date() < START_DATE:
        log(f"waiting for start date {START_DATE}")
        return
    log(f"budget: deposits ${led['deposits_usd']:.2f}, cash ${led['cash']:.2f}, equity ${equity:.2f}")
    manage(led, now)
    led, equity = snapshot(fx)  # refresh after any exits
    look_for_entry(led, now, equity)
    write_summary("```\n" + "\n".join(LOG) + "\n```\n\n" + report(led, equity, fx))


if __name__ == "__main__":
    main()
