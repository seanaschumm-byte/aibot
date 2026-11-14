"""Backtest MACD Nasdaq on QQQ daily bars: EUR 500 start + EUR 100 every week.
Compares: (1) the new version with dip buys + 12/18/25% profit rules, (2) plain MACD crossover,
(3) buying QQQ every week. Signals on the close -> orders at the next open; profit targets fill
intraday at the target price (or at the open if the price gapped past it).
  python -m macd_nasdaq.backtest [months]
"""
from __future__ import annotations

import math
import os
import sys

import pandas as pd
import requests

from .strategy import Rules, in_dip, lot_action, macd_state

EURUSD = 1.10
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}


def download(months: float) -> pd.DataFrame:
    start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(months * 30.44) + 200)).isoformat()
    params, rows = {"symbols": "QQQ", "timeframe": "1Day", "start": start, "limit": 10000,
                    "feed": "iex", "adjustment": "all"}, []
    while True:
        js = requests.get("https://data.alpaca.markets/v2/stocks/bars", headers=H, params=params, timeout=60).json()
        rows += (js.get("bars") or {}).get("QQQ", [])
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.normalize()
    return df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})


def simulate(df: pd.DataFrame, start: pd.Timestamp, mode: str, r: Rules = Rules()) -> dict:
    """mode: 'new' (dips + profit rules), 'macd' (plain crossover), 'dca' (buy every week)."""
    macd, dip = macd_state(df["close"], r), in_dip(df["close"], r)
    days = [t for t in df.index if t >= start]
    cash, deposited, lots, curve, closed, week = 500 * EURUSD, 500.0, [], [], [], None
    pending_buy = pending_exit = False
    slip = 0.0005

    def buy(t, usd, px):
        nonlocal cash
        if usd < 1:
            return
        cash -= usd
        lots.append({"qty": usd / (px * (1 + slip)), "entry": px * (1 + slip), "peak": px, "half": False,
                     "cost": usd, "proceeds": 0.0, "t": t})

    def sell(lot, qty, px, t):
        nonlocal cash
        qty = min(qty, lot["qty"])
        cash += qty * px * (1 - slip)
        lot["proceeds"] += qty * px * (1 - slip)
        lot["qty"] -= qty

    for i, t in enumerate(days):
        b = df.loc[t]
        dep = 0.0
        if i > 0 and t.isocalendar()[:2] != week:
            dep = 100 * EURUSD
            cash += dep
            deposited += 100
        week = t.isocalendar()[:2]
        prev = df.index[df.index.get_loc(t) - 1]
        long_, dip_ = bool(macd.loc[prev]), bool(dip.loc[prev])     # yesterday's close decides today
        # 1. orders at the open
        if pending_exit and mode in ("new", "macd"):
            for lot in lots:
                sell(lot, lot["qty"], b["open"], t)
        if mode == "dca":
            buy(t, cash, b["open"])
        elif mode == "macd":
            if long_:
                buy(t, cash, b["open"])
        else:
            if pending_buy:                            # new MACD buy signal or a new dip: all cash in
                buy(t, cash, b["open"])
            elif (long_ or dip_) and dep:              # weekly buy while MACD is up or in a dip: the new
                buy(t, cash, b["open"])                # deposit + any profit taken since the last buy
        pending_buy = pending_exit = False
        # 2. intraday profit rules (new version only)
        if mode == "new":
            for lot in lots:
                if lot["qty"] <= 1e-9:
                    continue
                e = lot["entry"]
                lot["peak"] = max(lot["peak"], b["high"])
                tp1, tp2, floor = e * (1 + r.tp1), e * (1 + r.tp2), e * (1 + r.floor)
                if not lot["half"] and b["high"] >= tp1:              # +12%: sell half
                    sell(lot, lot["qty"] * r.tp1_fraction, max(tp1, b["open"]), t)
                    lot["half"] = True
                if lot["half"] and b["high"] >= tp2:                  # +25%: sell the rest
                    sell(lot, lot["qty"], max(tp2, b["open"]), t)
                elif lot["half"] and lot["peak"] >= floor and b["low"] <= floor:   # back to +18%
                    sell(lot, lot["qty"], min(floor, b["open"]) if b["open"] < floor else floor, t)
        lots[:] = [l for l in lots if l["qty"] > 1e-9 or closed.append(l) or False]
        equity = cash + sum(l["qty"] * b["close"] for l in lots)
        curve.append((t, equity, dep))
        # 3. signals at the close -> tomorrow
        now_long, now_dip = bool(macd.loc[t]), bool(dip.loc[t])
        if mode == "new":
            pending_exit = long_ and not now_long and bool(lots)
            pending_buy = (now_long and not long_) or (now_dip and not dip_)
        elif mode == "macd":
            pending_exit = long_ and not now_long
    eq = pd.DataFrame(curve, columns=["t", "e", "d"])
    p = eq["e"].shift(1)
    ret = ((eq["e"] - eq["d"] - p) / p).fillna(0)
    twr = (1 + ret).cumprod()
    done = closed + [l for l in lots if l["proceeds"] > 0]
    wins = sum(1 for l in done if l["proceeds"] + l["qty"] * df["close"].iloc[-1] > l["cost"])
    return {"final": curve[-1][1] / EURUSD, "deposited": deposited, "ret": float(twr.iloc[-1] - 1),
            "dd": float((twr / twr.cummax() - 1).min()),
            "sharpe": float(ret.mean() / ret.std() * math.sqrt(252)) if ret.std() else 0.0,
            "buys": len(closed) + len(lots), "wins": wins, "closed": len(done),
            "cash_now": cash / EURUSD}


def main() -> None:
    months = float(sys.argv[1]) if len(sys.argv) > 1 else 8
    df = download(max(months, 8))
    start = df.index[-1] - pd.Timedelta(days=int(months * 30.44))
    lines = [f"## MACD Nasdaq backtest, last {months:g} months ({start:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}), "
             "EUR 500 + EUR 100/week", "",
             "| Version | Deposited | End value | Profit | Return (excl. deposits) | Max drawdown | Sharpe | Buys | Cash now |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, mode in (("**New: MACD + 12% dips + 12/18/25% profit rules**", "new"),
                       ("Plain MACD crossover", "macd"), ("Buy QQQ every week", "dca")):
        m = simulate(df, start, mode)
        lines.append(f"| {name} | EUR {m['deposited']:,.0f} | EUR {m['final']:,.0f} | EUR {m['final'] - m['deposited']:+,.0f} | "
                     f"**{m['ret']:+.1%}** | {m['dd']:.1%} | {m['sharpe']:.2f} | {m['buys']} | EUR {m['cash_now']:,.0f} |")
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
