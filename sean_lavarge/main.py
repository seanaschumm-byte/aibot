"""Sean Lavarge main loop.

  python -m sean_lavarge.main            one cycle (for a scheduler such as GitHub Actions)
  python -m sean_lavarge.main --loop     run forever, one cycle every LOOP_MINUTES (PC / VPS)

State (open positions, closed trades, pause timer, deposits) is kept in state/state.json.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time

from . import config as C
from . import notifier
from . import risk_manager as R
from .strategy import add_indicators, entry_signal, exit_signal, explain

log = logging.getLogger("sean_lavarge")


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        with open(C.STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"positions": {}, "closed": [], "realized": 0.0, "equity_hist": [],
                "paused_until": None, "last_weekly": None, "deposits": [], "last_balance": None}


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(C.STATE_FILE), exist_ok=True)
    tmp = C.STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1, default=str)
    os.replace(tmp, C.STATE_FILE)


# ---------------------------------------------------------------- equity
def compute_equity(ex, s: dict, now: dt.datetime, unrealized: float) -> tuple[float, float, float]:
    """(equity, risk_equity, free_margin) in the quote currency."""
    used_margin = sum(p["amount"] * p["avg"] / C.MAX_LEVERAGE for p in s["positions"].values())
    if C.CAPITAL_MODE == "virtual":
        deps = R.scheduled_deposits(now)
        deposited = sum(a for _, a in deps) * C.EURUSD
        equity = deposited + s["realized"] + unrealized
        locked = R.locked_amount(deps, now) * C.EURUSD
        risk_eq = equity - locked
        return equity, risk_eq, risk_eq - used_margin
    total, free = ex.equity()
    last = s.get("last_balance")
    if last is not None:   # deposit detection: a jump that trades don't explain
        unexplained = total - last["total"] - (s["realized"] - last["realized"]) - (unrealized - last["unreal"])
        if unexplained > 25:
            s["deposits"].append([now.isoformat(), unexplained])
            notifier.send(f"Deposit detected: +{unexplained:.2f} {C.QUOTE} (locked for {C.DEPOSIT_LOCK_DAYS} days)")
    s["last_balance"] = {"total": total, "realized": s["realized"], "unreal": unrealized}
    lock_from = now - dt.timedelta(days=C.DEPOSIT_LOCK_DAYS)
    locked = sum(a for t, a in s["deposits"] if dt.datetime.fromisoformat(t) > lock_from)
    return total, total - locked, free - locked


def equity_24h_ago(s: dict, now: dt.datetime) -> float | None:
    window = [e for t, e in s["equity_hist"]
              if dt.datetime.fromisoformat(t) >= now - dt.timedelta(hours=24)]
    return window[0] if window else None


# ---------------------------------------------------------------- trade helpers
def record_close(s: dict, symbol: str, exit_px: float, reason: str, now: dt.datetime,
                 pnl: float | None = None) -> None:
    p = s["positions"].pop(symbol)
    d = 1 if p["side"] == "long" else -1
    if pnl is None:
        fees = C.TAKER_FEE * p["amount"] * (p["avg"] + exit_px)
        pnl = (exit_px - p["avg"]) * p["amount"] * d - fees
    s["realized"] += pnl
    s["closed"].append({"symbol": symbol, "side": p["side"], "entry": p["avg"], "exit": exit_px,
                        "amount": p["amount"], "pnl": round(pnl, 4), "reason": reason,
                        "opened": p["opened"], "closed": now.isoformat()})
    notifier.send(f"{'STOP HIT' if reason == 'stop' else 'EXIT'} {symbol} {p['side']} @ {exit_px:.2f} "
                  f"({reason}) PnL {pnl:+.2f} {C.QUOTE}")


def open_position(ex, s, symbol, side, row, risk_eq, free_margin, now) -> None:
    price = float(row["close"])
    size = R.position_size(risk_eq, float(row["atr"]), price, free_margin)
    if size.amount <= 0 or size.amount < ex.min_amount(symbol):
        log.info("%s %s signal but size %.6f below exchange minimum", symbol, side, size.amount)
        return
    lev = R.leverage_for(size.notional, risk_eq)
    ex.setup_leverage(symbol, lev)
    order = ex.market(symbol, "buy" if side == "long" else "sell", size.amount)
    fill = float(order.get("average") or order.get("price") or ex.price(symbol))
    stop = R.stop_price(fill, float(row["atr"]), side)
    ex.place_stop(symbol, side, size.amount, stop)
    s["positions"][symbol] = {"side": side, "first_entry": fill, "avg": fill, "amount": size.amount,
                              "first_amount": size.amount, "atr": float(row["atr"]), "stop": stop,
                              "pyramids": 0, "opened": now.isoformat(), "leverage": lev}
    notifier.send(f"ENTRY {symbol} {side.upper()} {size.amount:.5f} @ {fill:.2f}, stop {stop:.2f}, "
                  f"{lev}x isolated, risk {size.risk:.2f} {C.QUOTE} ({C.RISK_PER_TRADE:.1%})"
                  + (" [size capped by 10x]" if size.capped else "") + f" | {explain(row)}")


def pyramid(ex, s, symbol, price, risk_eq, free_margin) -> None:
    p = s["positions"][symbol]
    add = p["first_amount"] * C.PYRAMID_FRACTION
    if (p["amount"] + add) * price > risk_eq * C.MAX_LEVERAGE or add * price / C.MAX_LEVERAGE > free_margin:
        log.info("%s pyramid skipped: would exceed %dx / free margin", symbol, C.MAX_LEVERAGE)
        return
    order = ex.market(symbol, "buy" if p["side"] == "long" else "sell", add)
    fill = float(order.get("average") or price)
    p["avg"] = (p["avg"] * p["amount"] + fill * add) / (p["amount"] + add)
    p["amount"] += add
    p["pyramids"] += 1
    p["stop"] = p["first_entry"]                     # breakeven of the first unit
    ex.cancel_stops(symbol)
    ex.place_stop(symbol, p["side"], p["amount"], p["stop"])
    notifier.send(f"PYRAMID {symbol} +{add:.5f} @ {fill:.2f}; stop moved to breakeven {p['stop']:.2f} "
                  f"for {p['amount']:.5f}")


# ---------------------------------------------------------------- one cycle
def run_once(ex, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = load_state()

    data, positions_ex, unreal = {}, {}, 0.0
    for sym in C.SYMBOLS:
        data[sym] = add_indicators(ex.ohlcv(sym))
        positions_ex[sym] = ex.position(sym)
        unreal += (positions_ex[sym] or {}).get("unrealized", 0.0)

    # stops that fired on the exchange since the last cycle
    for sym in list(s["positions"]):
        if not positions_ex.get(sym):
            p = s["positions"][sym]
            since = int(dt.datetime.fromisoformat(p["opened"]).timestamp() * 1000)
            pnl = ex.realized_since(sym, since)
            record_close(s, sym, p["stop"], "stop", now, pnl)

    equity, risk_eq, free = compute_equity(ex, s, now, unreal)
    s["equity_hist"] = [e for e in s["equity_hist"]
                        if dt.datetime.fromisoformat(e[0]) >= now - dt.timedelta(hours=48)]
    s["equity_hist"].append([now.isoformat(), equity])
    log.info("equity %.2f %s (risk equity %.2f, free margin %.2f)", equity, C.QUOTE, risk_eq, free)

    wk = f"{now.isocalendar()[0]}W{now.isocalendar()[1]:02d}"
    if s.get("last_weekly") != wk:
        s["last_weekly"] = wk
        notifier.send(f"Weekly update {wk}: equity {equity:.2f} {C.QUOTE} (~EUR {equity / C.EURUSD:.2f}), "
                      f"open positions {len(s['positions'])}, closed trades {len(s['closed'])}, "
                      f"realised {s['realized']:+.2f}")

    paused = bool(s.get("paused_until") and dt.datetime.fromisoformat(s["paused_until"]) > now)
    if not paused:
        hit, why = R.breaker(s["closed"], equity_24h_ago(s, now), equity, now)
        if hit:
            s["paused_until"] = (now + dt.timedelta(hours=C.PAUSE_HOURS)).isoformat()
            paused = True
            notifier.send(f"CIRCUIT BREAKER: {why}. No new trades until {s['paused_until'][:16]} UTC.")
    if paused:
        log.info("paused until %s: managing open positions only", s["paused_until"])

    for sym in C.SYMBOLS:
        df = data[sym]
        if len(df) < C.EMA_TREND + 1:
            log.info("%s: not enough candles yet", sym)
            continue
        row = df.iloc[-1]
        pos_ex, p = positions_ex.get(sym), s["positions"].get(sym)
        if pos_ex and not p:   # position the bot didn't know (e.g. after a crash): adopt + protect
            atr = float(row["atr"])
            s["positions"][sym] = p = {"side": pos_ex["side"], "first_entry": pos_ex["entry"],
                                       "avg": pos_ex["entry"], "amount": pos_ex["amount"],
                                       "first_amount": pos_ex["amount"], "atr": atr,
                                       "stop": R.stop_price(pos_ex["entry"], atr, pos_ex["side"]),
                                       "pyramids": C.MAX_PYRAMIDS, "opened": now.isoformat(), "leverage": 0}
            notifier.send(f"Adopted unknown {sym} position; stop set at {p['stop']:.2f}")
        if p and pos_ex:
            price = ex.price(sym)
            if exit_signal(row, p["side"]):
                ex.cancel_stops(sym)
                order = ex.market(sym, "sell" if p["side"] == "long" else "buy", pos_ex["amount"], True)
                record_close(s, sym, float(order.get("average") or price), "turtle 10-bar exit", now)
            elif not paused and R.pyramid_due(p["first_entry"], p["atr"], price, p["side"], p["pyramids"]):
                pyramid(ex, s, sym, price, risk_eq, free)
            elif not ex.has_stop(sym):
                ex.place_stop(sym, p["side"], pos_ex["amount"], p["stop"])
                notifier.send(f"Stop for {sym} was missing: re-placed at {p['stop']:.2f}")
            log.info("%s %s %.5f avg %.2f now %.2f stop %.2f", sym, p["side"], p["amount"], p["avg"],
                     price, p["stop"])
        elif not p and not paused and len(s["positions"]) < C.MAX_OPEN_POSITIONS:
            sig = entry_signal(row)
            log.info("%s: %s | %s", sym, sig or "no signal", explain(row))
            bar = str(row.name)
            if sig and s.setdefault("last_entry_bar", {}).get(sym) == bar:
                log.info("%s: already traded this candle (%s), waiting for a new one", sym, bar)
            elif sig:
                s["last_entry_bar"][sym] = bar
                open_position(ex, s, sym, sig, row, risk_eq, free, now)

    save_state(s)
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (os.getenv("EXCHANGE_API_KEY") and os.getenv("EXCHANGE_SECRET")):
        log.error("EXCHANGE_API_KEY / EXCHANGE_SECRET not set: create %s API keys and add them as secrets.",
                  "DEMO" if C.TESTNET else "LIVE")
        return
    from .exchange_client import Exchange
    log.info("Sean Lavarge starting on %s (%s)", C.EXCHANGE_ID, "TESTNET / demo" if C.TESTNET else "LIVE MONEY")
    ex = Exchange()
    while True:
        try:
            run_once(ex)
        except Exception as e:                      # never die silently
            log.exception("cycle failed")
            notifier.send(f"ERROR: {type(e).__name__}: {e}")
            if "--loop" not in sys.argv:
                raise
        if "--loop" not in sys.argv:
            return
        time.sleep(C.LOOP_MINUTES * 60)


if __name__ == "__main__":
    main()
