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


def download(months: float, symbol: str = "QQQ") -> pd.DataFrame:
    start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(months * 30.44) + 200)).isoformat()
    crypto = "/" in symbol
    url = "https://data.alpaca.markets/" + ("v1beta3/crypto/us/bars" if crypto else "v2/stocks/bars")
    params, rows = {"symbols": symbol, "timeframe": "1Day", "start": start, "limit": 10000}, []
    if not crypto:
        params.update({"feed": "iex", "adjustment": "all"})
    while True:
        js = requests.get(url, headers=H, params=params, timeout=60).json()
        rows += (js.get("bars") or {}).get(symbol, [])
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True).dt.normalize()
    return df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})


def simulate(df: pd.DataFrame, start: pd.Timestamp, mode: str, r: Rules = Rules(), start_eur: float = 500.0,
             weekly_eur: float = 100.0, fee: float = 0.0, core_sell: float = 1.0, core_tp: float = 0.60) -> dict:
    """mode: 'new' (dips + profit rules), 'macd' (plain crossover), 'dca' (buy every week),
    'core' (buy every week; sell everything at +60% on the average cost; buy back when the price is
    30% under the highest price since that sale)."""
    macd, dip = macd_state(df["close"], r), in_dip(df["close"], r)
    days = [t for t in df.index if t >= start]
    cash, deposited, lots, curve, closed, week = start_eur * EURUSD, start_eur, [], [], [], None
    pending_buy = pending_exit = False
    slip = 0.0005 + fee
    sold, core_events, reserve = None, [], 0.0   # reserve: money from a partial core sale, waiting for -30%

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
            dep = weekly_eur * EURUSD
            cash += dep
            deposited += weekly_eur
        week = t.isocalendar()[:2]
        prev = df.index[df.index.get_loc(t) - 1]
        long_, dip_ = bool(macd.loc[prev]), bool(dip.loc[prev])     # yesterday's close decides today
        # 1. orders at the open
        if pending_exit and mode in ("new", "macd"):
            for lot in lots:
                sell(lot, lot["qty"], b["open"], t)
        if mode == "dca" or (mode == "core" and sold is None and (dep or i == 0)):
            buy(t, cash, b["open"])
        elif mode == "core" and sold is not None and core_sell < 1 and dep:
            buy(t, cash - reserve, b["open"])       # partial sale: weekly buys go on, the reserve waits
        elif mode == "macd":
            if long_:
                buy(t, cash, b["open"])
        else:
            if pending_buy:                            # new MACD buy signal or a new dip: all cash in
                buy(t, cash, b["open"])
            elif (long_ or dip_) and dep:              # weekly buy while MACD is up or in a dip: the new
                buy(t, cash, b["open"])                # deposit + any profit taken since the last buy
        pending_buy = pending_exit = False
        # 2a. core: +60% -> sell all; then buy back 30% under the highest price since the sale
        if mode == "core":
            held = [l for l in lots if l["qty"] > 1e-9]
            if sold is None and held:
                avg = sum(l["qty"] * l["entry"] for l in held) / sum(l["qty"] for l in held)
                if b["high"] >= avg * (1 + core_tp):
                    px = max(avg * (1 + core_tp), b["open"])
                    before = cash
                    for lot in held:
                        sell(lot, lot["qty"] * core_sell, px, t)
                    reserve = cash - before
                    sold = {"peak": px}
                    core_events.append(f"{t:%Y-%m-%d} sold {core_sell:.0%} at {px:.2f} (+{core_tp:.0%})")
            elif sold is not None:
                sold["peak"] = max(sold["peak"], b["high"])
                trigger = sold["peak"] * 0.70
                if b["low"] <= trigger:
                    px = min(trigger, b["open"])
                    buy(t, cash, px)
                    reserve = 0.0
                    core_events.append(f"{t:%Y-%m-%d} bought back at {px:.2f} (-30% from {sold['peak']:.2f})")
                    sold = None
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
            "cash_now": cash / EURUSD, "curve": curve, "core_events": core_events}


ALL_MARKETS = {"QQQ": ("QQQ", 63, 0.0), "BTC": ("BTC/USD", 90, 0.0025), "ETH": ("ETH/USD", 90, 0.0025),
               "SOL": ("SOL/USD", 90, 0.0025)}
# MACD Nasdaq = QQQ; Botsi = BTC,ETH,SOL (set MARKETS=BTC,ETH,SOL)
MARKETS = {t: ALL_MARKETS[t] for t in os.getenv("MARKETS", "QQQ").split(",")}
TITLE = os.getenv("TITLE", "MACD Nasdaq")
SAT_LABEL = "plain MACD" if os.getenv("SAT_MODE") == "macd" else "MACD + dips + profit rules"


def combine(curves: list[list[tuple]]) -> dict:
    """Sum the sleeves' equity day by day (stocks are carried over weekends) -> one portfolio."""
    frames = []
    for c in curves:
        f = pd.DataFrame(c, columns=["t", "e", "d"]).set_index("t")
        frames.append(f)
    idx = sorted(set().union(*[f.index for f in frames]))
    e = sum(f["e"].reindex(idx).ffill().fillna(0) for f in frames)
    d = sum(f["d"].reindex(idx).fillna(0) for f in frames)
    p = e.shift(1)
    ret = ((e - d - p) / p).replace([float("inf")], 0).fillna(0)
    twr = (1 + ret).cumprod()
    return {"final": float(e.iloc[-1]) / EURUSD, "ret": float(twr.iloc[-1] - 1),
            "dd": float((twr / twr.cummax() - 1).min()),
            "sharpe": float(ret.mean() / ret.std() * math.sqrt(365)) if ret.std() else 0.0}


def main() -> None:
    months = float(sys.argv[1]) if len(sys.argv) > 1 else 8
    data = {tag: download(max(months, 8), sym) for tag, (sym, _, _) in MARKETS.items()}
    end = min(df.index[-1] for df in data.values())
    data = {tag: df.loc[:end] for tag, df in data.items()}            # same end date for every market
    start = end - pd.Timedelta(days=int(months * 30.44))
    share = 1 / len(MARKETS)
    lines = [f"## {TITLE} backtest ({', '.join(MARKETS)}), last {months:g} months ({start:%Y-%m-%d} to {end:%Y-%m-%d}), "
             f"EUR 500 + EUR 100/week, {share:.0%} per market", "",
             "| Version | Deposited | End value | Profit | Return (excl. deposits) | Max drawdown | Sharpe |",
             "|---|---|---|---|---|---|---|"]
    per_market = []
    for name, mode in (("**New: MACD + 12% dips + 12/18/25% profit rules**", "new"),
                       ("Plain MACD crossover", "macd"), ("Buy every week", "dca")):
        runs = {tag: simulate(data[tag], start, mode, Rules(dip_lookback=lb), 500.0 * share, 100.0 * share, fee)
                for tag, (_, lb, fee) in MARKETS.items()}
        tot = combine([r["curve"] for r in runs.values()])
        dep = sum(r["deposited"] for r in runs.values())
        lines.append(f"| {name} | EUR {dep:,.0f} | EUR {tot['final']:,.0f} | EUR {tot['final'] - dep:+,.0f} | "
                     f"**{tot['ret']:+.1%}** | {tot['dd']:.1%} | {tot['sharpe']:.2f} |")
        if mode == "new":
            per_market = [f"{tag} {r['ret']:+.1%} (EUR {r['final'] - r['deposited']:+,.0f})" for tag, r in runs.items()]
    # core + satellite: 60% core, 40% the new MACD rules. Core variants:
    #   current: sell 100% at +60%, buy back at -30%   C: sell 25% at +60%, rest keeps running   A: never sell
    events = []
    for label, sell_frac, tp in (("Core 60% (sells 100% at +60%) + satellite 40%", 1.0, 0.60),
                                 ("Core 60% (sells 25% at +60%) + satellite 40%", 0.25, 0.60),
                                 (f"**Core 60% never sells + satellite 40% {SAT_LABEL} (live)**", 1.0, 99.0)):
        core_runs, sat_runs = {}, {}
        for tag, (_, lb, fee) in MARKETS.items():
            core_runs[tag] = simulate(data[tag], start, "core", Rules(dip_lookback=lb), 300.0 * share, 60.0 * share,
                                      fee, sell_frac, tp)
            sat_runs[tag] = simulate(data[tag], start, os.getenv("SAT_MODE", "new"), Rules(dip_lookback=lb),
                                     200.0 * share, 40.0 * share, fee)   # Botsi: SAT_MODE=macd (plain MACD)
        tot = combine([r["curve"] for r in list(core_runs.values()) + list(sat_runs.values())])
        dep = sum(r["deposited"] for r in list(core_runs.values()) + list(sat_runs.values()))
        lines.append(f"| {label} | EUR {dep:,.0f} | EUR {tot['final']:,.0f} | EUR {tot['final'] - dep:+,.0f} | "
                     f"**{tot['ret']:+.1%}** | {tot['dd']:.1%} | {tot['sharpe']:.2f} |")
        events += [f"{label.strip('*').split(':')[0]} {tag}: " + "; ".join(r["core_events"])
                   for tag, r in core_runs.items() if r["core_events"]]
    lines += ["", "New version per market: " + ", ".join(per_market),
              "Core +60% / -30% events: " + (" | ".join(events) if events else "none (the +60% level was never reached)")]
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
