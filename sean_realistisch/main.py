"""Sean Realistisch: one cycle per run (GitHub Actions every 30 min during market hours), or
`python -m sean_realistisch.main --loop` on your own PC.

Truth about holdings comes from this bot's own Alpaca orders (client_order_id prefix "sr-");
extra per-position info (initial ATR, stop, pyramid / take-profit flags) is kept in state/state.json.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time

from . import config as C
from . import indicators as I
from . import notifier
from . import risk_manager as R

log = logging.getLogger("sean_realistisch")


# ---------------------------------------------------------------- state & ledger
def load_state() -> dict:
    try:
        with open(C.STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"positions": {}, "closed": [], "equity_hist": [], "paused_until": None,
                "deposits_seen": 0, "last_entry_day": {}}


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(C.STATE_FILE), exist_ok=True)
    tmp = C.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, default=str)
    os.replace(tmp, C.STATE_FILE)


def order_dict(o) -> dict:
    return {"symbol": o.symbol, "side": str(o.side).lower().split(".")[-1],
            "qty": float(o.filled_qty or 0), "price": float(o.filled_avg_price or 0),
            "cid": o.client_order_id, "time": str(o.filled_at or o.submitted_at)}


def ledger(orders: list[dict], deposited: float) -> dict:
    """Cash, holdings and finished trades of this bot only."""
    cash, qty, cost, closes = deposited, {}, {}, []
    for o in orders:
        if o["qty"] <= 0:
            continue
        s = o["symbol"]
        if o["side"] == "buy":
            cash -= o["qty"] * o["price"]
            qty[s] = qty.get(s, 0) + o["qty"]
            cost[s] = cost.get(s, 0) + o["qty"] * o["price"]
        else:
            cash += o["qty"] * o["price"]
            share = min(1.0, o["qty"] / qty[s]) if qty.get(s) else 1.0
            part = cost.get(s, 0) * share
            qty[s] = qty.get(s, 0) - o["qty"]
            cost[s] = cost.get(s, 0) - part
            closes.append({"cid": o["cid"], "symbol": s, "pnl": o["qty"] * o["price"] - part,
                           "time": o["time"], "full": qty[s] <= 1e-6})
            if qty[s] <= 1e-6:
                qty.pop(s), cost.pop(s, None)
    return {"cash": cash, "qty": qty, "cost": cost, "closes": closes}


# ---------------------------------------------------------------- helpers
def deposits_now(broker, today: dt.date) -> list[tuple[str, float]]:
    if C.CAPITAL_MODE == "virtual":
        return [(d.isoformat(), a) for d, a in R.scheduled_deposits(today)]
    return broker.cash_deposits_since(C.START_DATE)


def sell(broker, s, sym: str, qty: float, why: str, now: dt.datetime) -> None:
    broker.cancel_stops(sym)
    q = int(qty * 1e6) / 1e6
    if q <= 0:
        return
    o = broker.sell_qty(sym, q, f"{C.PREFIX}-X-{sym}-{int(time.time())}")
    o = broker.wait_filled(o.id)
    px = float(o.filled_avg_price or 0) if o else 0
    notifier.send(f"SELL {q:.4f} {sym} @ {px:.2f}: {why}")


def ensure_stop(broker, sym: str, qty: float, stop: float) -> None:
    """Day stop for the full own quantity (fractional shares can't use GTC stops)."""
    stops = broker.open_stops(sym)
    ok = len(stops) == 1 and abs(float(stops[0].stop_price) - round(stop, 2)) < 0.011 \
        and abs(float(stops[0].qty) - round(qty, 6)) < 1e-5
    if not ok:
        broker.cancel_stops(sym)
        broker.place_stop(sym, int(qty * 1e6) / 1e6, stop, f"{C.PREFIX}-S-{sym}-{int(time.time())}")


# ---------------------------------------------------------------- one cycle
def run_once(broker, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    s = load_state()
    deps = deposits_now(broker, today)
    deposited = sum(a for _, a in deps)
    for d, a in deps[s.get("deposits_seen", 0):]:
        notifier.send(f"Deposit {'scheduled' if C.CAPITAL_MODE == 'virtual' else 'detected'}: +${a:.2f} on {d}")
    s["deposits_seen"] = len(deps)

    led = ledger([order_dict(o) for o in broker.my_orders(C.START_DATE)], deposited)
    prices = {sym: broker.price(sym) for sym in led["qty"]}
    if C.CAPITAL_MODE == "virtual":
        equity, cash = led["cash"] + sum(q * prices[x] for x, q in led["qty"].items()), led["cash"]
    else:
        equity, cash = broker.account_equity()

    # stop-losses that filled on Alpaca since the last run
    for sleeve, p in list(s["positions"].items()):
        if p["symbol"] not in led["qty"]:
            last = [c for c in led["closes"] if c["symbol"] == p["symbol"]]
            pnl = sum(c["pnl"] for c in last if c["time"] >= p["opened"])
            s["closed"].append({"sleeve": sleeve, "symbol": p["symbol"], "pnl": round(pnl, 2),
                                "opened": p["opened"], "closed": now.isoformat()})
            s["positions"].pop(sleeve)
            notifier.send(f"STOP-LOSS / exit filled: {p['symbol']} closed, P&L ${pnl:+.2f}")

    # equity history for the 7-day breaker (adjusted for deposits)
    s["equity_hist"] = [e for e in s["equity_hist"]
                        if dt.date.fromisoformat(e[0]) >= today - dt.timedelta(days=C.BREAKER_DAYS)]
    if not s["equity_hist"] or s["equity_hist"][-1][0] != today.isoformat():
        s["equity_hist"].append([today.isoformat(), equity, deposited])
    else:
        s["equity_hist"][-1] = [today.isoformat(), max(s["equity_hist"][-1][1], equity), deposited]
    adjusted = [(d, e + (deposited - dep)) for d, e, dep in s["equity_hist"]]
    log.info("equity $%.2f (cash $%.2f, deposited $%.2f)", equity, cash, deposited)

    is_open, why = broker.market_open_now()
    if not is_open:
        log.info("no trading: %s", why)
        save_state(s)
        return s

    paused = bool(s.get("paused_until") and dt.datetime.fromisoformat(s["paused_until"]) > now)
    if not paused:
        hit, reason = R.breaker(adjusted, equity)
        if hit:
            s["paused_until"] = (now + dt.timedelta(hours=C.PAUSE_HOURS)).isoformat()
            paused = True
            notifier.send(f"CIRCUIT BREAKER: {reason}. Closing 2x ETFs, no new entries until {s['paused_until'][:16]} UTC")
            for sleeve, p in list(s["positions"].items()):
                if p["symbol"] in C.LEVERAGED and p["symbol"] in led["qty"]:
                    sell(broker, s, p["symbol"], led["qty"][p["symbol"]], "circuit breaker", now)

    syms = sorted({v["index"] for v in C.SLEEVES.values()} |
                  {x for v in C.SLEEVES.values() for x in (v["x1"], v["x2"]) if x})
    bars = {k: I.add(v) for k, v in broker.daily_bars(syms).items()}

    # manage open positions
    for sleeve, p in list(s["positions"].items()):
        sym, qty = p["symbol"], led["qty"].get(p["symbol"], 0)
        if qty <= 0:
            continue
        price = broker.price(sym)
        idx = bars[C.SLEEVES[sleeve]["index"]]
        row, prev = idx.iloc[-1], idx.iloc[-2]
        reg = I.regime(row, prev)
        if C.EXIT_WHEN_REGIME_OFF and reg == "off":
            sell(broker, s, sym, qty, f"{C.SLEEVES[sleeve]['index']} left the golden-cross regime -> cash", now)
            continue
        tp = R.take_profit_due(p["entry"], p["atr"], price, float(row["rsi2"]), p["partial"])
        if tp:
            part = qty * C.TP_FRACTION
            sell(broker, s, sym, part, f"take 50% profit ({tp})", now)
            qty -= part
            p["partial"], p["highest"] = True, max(p.get("highest", price), price)
        elif not paused and R.pyramid_due(p["entry"], p["atr"], price, p["pyramided"]):
            add = min(p["first_value"] * C.PYRAMID_FRACTION, cash,
                      C.MAX_POSITION_PCT * equity - qty * price)
            if add >= 1:
                o = broker.wait_filled(broker.buy_notional(sym, add, f"{C.PREFIX}-P-{sym}-{int(time.time())}").id)
                qty += float(o.filled_qty or 0)
                p["pyramided"], p["stop"] = True, max(p["stop"], p["entry"])
                notifier.send(f"PYRAMID {sym} +${add:.2f} @ {float(o.filled_avg_price or price):.2f}; "
                              f"stop to breakeven {p['stop']:.2f}")
        if p["partial"]:
            p["highest"] = max(p.get("highest", price), price)
            new = R.trailing_stop(p["stop"], p["highest"], p["atr"])
            if new > p["stop"] * 1.002:
                p["stop"] = new
                notifier.send(f"TRAILING STOP {sym} raised to {new:.2f} (high {p['highest']:.2f})")
        if qty > 0:
            ensure_stop(broker, sym, qty, p["stop"])
        log.info("%s %s qty %.4f entry %.2f now %.2f stop %.2f regime %s", sleeve, sym, qty, p["entry"],
                 price, p["stop"], reg)

    # new entries (signals from yesterday's completed daily bar)
    for sleeve, cfg in C.SLEEVES.items():
        if paused or len(s["positions"]) >= C.MAX_POSITIONS or sleeve in s["positions"]:
            continue
        if s["last_entry_day"].get(sleeve) == today.isoformat():
            continue
        idx = bars.get(cfg["index"])
        if idx is None or len(idx) < C.SMA_SLOW + 2:
            continue
        row, prev = idx.iloc[-1], idx.iloc[-2]
        reg = I.regime(row, prev)
        sig = I.entry(row) if reg != "off" else None
        log.info("%s: regime %s, signal %s (close %.2f, RSI2 %.0f, SMA50 %.2f, SMA200 %.2f)", sleeve, reg, sig,
                 row["close"], row["rsi2"], row["sma50"], row["sma200"])
        if not sig:
            continue
        sym = cfg["x2"] if reg == "2x" and cfg["x2"] else cfg["x1"]
        atr = float(bars[sym]["atr"].iloc[-1])
        price = broker.price(sym)
        value = R.position_value(equity, atr, price, cash)
        if value < 1:
            log.info("%s: position too small ($%.2f)", sym, value)
            continue
        s["last_entry_day"][sleeve] = today.isoformat()
        o = broker.wait_filled(broker.buy_notional(sym, value, f"{C.PREFIX}-B-{sym}-{int(time.time())}").id)
        q, fill = float(o.filled_qty or 0), float(o.filled_avg_price or price)
        if q <= 0:
            notifier.send(f"BUY {sym} not filled ({o.status})")
            continue
        stop = R.initial_stop(fill, atr)
        broker.place_stop(sym, int(q * 1e6) / 1e6, stop, f"{C.PREFIX}-S-{sym}-{int(time.time())}")
        s["positions"][sleeve] = {"symbol": sym, "entry": fill, "atr": atr, "first_value": value, "stop": stop,
                                  "pyramided": False, "partial": False, "highest": fill,
                                  "opened": now.isoformat(), "signal": sig, "regime": reg}
        cash -= value
        notifier.send(f"BUY {sym} ${value:.2f} ({q:.4f} sh @ {fill:.2f}), {sig} in {reg} regime; "
                      f"stop {stop:.2f} (risk ${q * (fill - stop):.2f} = {C.RISK_PER_TRADE:.0%})")

    save_state(s)
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")):
        log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return
    from .broker_alpaca import Broker
    log.info("Sean Realistisch starting (%s, capital mode %s)", "PAPER" if C.PAPER else "LIVE", C.CAPITAL_MODE)
    broker = Broker()
    while True:
        try:
            run_once(broker)
        except Exception as e:
            log.exception("cycle failed")
            notifier.send(f"ERROR: {type(e).__name__}: {e}")
            if "--loop" not in sys.argv:
                raise
        if "--loop" not in sys.argv:
            return
        time.sleep(15 * 60)


if __name__ == "__main__":
    main()
