"""Backtest Sean Realistisch on daily bars: $500 start + $300 on the 1st of every month.

  python -m sean_realistisch.backtest              24 months of Alpaca daily data (needs API keys)
  python -m sean_realistisch.backtest --csv DIR    DIR/<SYMBOL>.csv with date,open,high,low,close,volume

Same rules as the live bot: signals on the daily close, orders at the next open, stops / pyramid /
2.5R take-profit intraday, RSI(2) take-profit and trailing stop on the close, regime exits, 7-day
-6% circuit breaker. Outputs CAGR, Win Rate, Profit Factor, Max Drawdown and Sharpe (time-weighted,
so deposits don't count as profit), plus a 'same deposits into SPY' benchmark.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys

import numpy as np
import pandas as pd

from . import config as C
from . import indicators as I
from . import risk_manager as R

SYMBOLS = ["SPY", "QQQ", "SSO", "QLD", "GLD"]


def download(months: int) -> dict[str, pd.DataFrame]:
    from .broker_alpaca import Broker
    days = int(months * 30.5) + 320          # + warm-up for the 200-day SMA
    return Broker().daily_bars(SYMBOLS, days)


def simulate(data: dict[str, pd.DataFrame], months: int = C.BACKTEST_MONTHS) -> dict:
    data = {k: I.add(v) for k, v in data.items()}
    dates = sorted(set.intersection(*[set(v.index) for v in data.values()]))
    start = dates[-1] - pd.Timedelta(days=int(months * 30.44))
    dates = [d for d in dates if d >= start and not pd.isna(data["SPY"].loc[d, "sma200"])]
    slip = 0.0005                                     # 0.05% per fill (spread/slippage)

    cash, deposits = C.START_CAPITAL, [(dates[0], C.START_CAPITAL)]
    pos: dict[str, dict] = {}                         # sleeve -> position
    pending: list[tuple] = []
    trades, curve, paused_until, pauses, month = [], [], None, 0, (dates[0].year, dates[0].month)
    spy_units, spy_cash = 0.0, 0.0                    # benchmark

    def mark(t):
        return sum(p["qty"] * data[p["symbol"]].loc[t, "close"] for p in pos.values())

    def sell(sleeve, qty, px, t, why):
        nonlocal cash
        p = pos[sleeve]
        qty = min(qty, p["qty"])
        px *= (1 - slip)
        cash += qty * px
        p["proceeds"] += qty * px
        p["qty"] -= qty
        if p["qty"] <= 1e-9:
            pnl = p["proceeds"] - p["cost"]
            trades.append({"sleeve": sleeve, "symbol": p["symbol"], "pnl": pnl, "opened": p["opened"],
                           "closed": t, "why": why, "R": pnl / p["risk"] if p["risk"] else 0})
            pos.pop(sleeve)

    def buy(sleeve, sym, value, px, t, atr=None, sig=None):
        nonlocal cash
        value = min(value, cash)
        if value < 1:
            return
        px *= (1 + slip)
        q = value / px
        cash -= value
        if sleeve in pos:                             # pyramid
            p = pos[sleeve]
            p["qty"] += q
            p["cost"] += value
            p["pyramided"] = True
            p["stop"] = max(p["stop"], p["entry"])
        else:
            pos[sleeve] = {"symbol": sym, "qty": q, "entry": px, "atr": atr, "stop": R.initial_stop(px, atr),
                           "first_value": value, "cost": value, "proceeds": 0.0, "pyramided": False,
                           "partial": False, "highest": px, "opened": t, "sig": sig,
                           "risk": q * C.STOP_ATR * atr}

    for i, t in enumerate(dates):
        dep = 0.0
        if (t.year, t.month) != month:                # first trading day of a new month
            month = (t.year, t.month)
            cash += C.MONTHLY_DEPOSIT
            dep = C.MONTHLY_DEPOSIT
            deposits.append((t, dep))
        # benchmark: every deposit straight into SPY at the open
        spy_cash += (C.START_CAPITAL if i == 0 else 0) + dep
        spy_units += spy_cash / data["SPY"].loc[t, "open"]
        spy_cash = 0.0

        # 1. yesterday's decisions at today's open
        for kind, sleeve, *args in pending:
            o = lambda s: data[s].loc[t, "open"]
            if kind == "exit" and sleeve in pos:
                sell(sleeve, pos[sleeve]["qty"], o(pos[sleeve]["symbol"]), t, args[0])
            elif kind == "partial" and sleeve in pos and not pos[sleeve]["partial"]:
                pos[sleeve]["partial"] = True
                sell(sleeve, pos[sleeve]["qty"] * C.TP_FRACTION, o(pos[sleeve]["symbol"]), t, "tp")
            elif kind == "entry" and sleeve not in pos and len(pos) < C.MAX_POSITIONS:
                sym, value, atr, sig = args
                buy(sleeve, sym, value, o(sym), t, atr, sig)
        pending = []

        # 2. intraday: stop, pyramid, 2.5R take-profit
        for sleeve, p in list(pos.items()):
            b = data[p["symbol"]].loc[t]
            if b["low"] <= p["stop"]:
                sell(sleeve, p["qty"], min(b["open"], p["stop"]), t, "stop")
                continue
            trig = p["entry"] + C.PYRAMID_ATR * p["atr"]
            if not p["pyramided"] and b["high"] >= trig and not (paused_until and t < paused_until):
                equity_est = cash + mark(t)
                add = min(p["first_value"] * C.PYRAMID_FRACTION, C.MAX_POSITION_PCT * equity_est - p["qty"] * trig)
                if add >= 1:
                    buy(sleeve, p["symbol"], add, trig, t)
            tp = p["entry"] + C.TP_R_MULTIPLE * C.STOP_ATR * p["atr"]
            if not p["partial"] and b["high"] >= tp and sleeve in pos:
                p["partial"] = True
                sell(sleeve, p["qty"] * C.TP_FRACTION, max(tp, b["open"]), t, "tp 2.5R")

        equity = cash + mark(t)
        curve.append((t, equity, dep, spy_units * data["SPY"].loc[t, "close"]))

        # 3. circuit breaker on the close (deposit-adjusted 7-day high)
        if not (paused_until and t < paused_until):
            window = [(d, e) for d, e, _, _ in curve if d >= t - pd.Timedelta(days=C.BREAKER_DAYS)]
            later_deps = lambda d: sum(a for dd, a in deposits if dd > d)
            hit, _ = R.breaker([(d, e + later_deps(d)) for d, e in window], equity)
            if hit:
                paused_until, pauses = t + pd.Timedelta(hours=C.PAUSE_HOURS), pauses + 1
                for sleeve, p in pos.items():
                    if p["symbol"] in C.LEVERAGED:
                        pending.append(("exit", sleeve, "breaker"))
        paused = bool(paused_until and t < paused_until)

        # 4. close-based management and new signals
        for sleeve, cfg in C.SLEEVES.items():
            idx = data[cfg["index"]]
            row, prev = idx.loc[t], idx.iloc[idx.index.get_loc(t) - 1]
            reg = I.regime(row, prev)
            if sleeve in pos:
                p = pos[sleeve]
                close_px = data[p["symbol"]].loc[t, "close"]
                if C.EXIT_WHEN_REGIME_OFF and reg == "off":
                    pending.append(("exit", sleeve, "regime off"))
                    continue
                if not p["partial"] and row["rsi2"] > C.TP_RSI:
                    pending.append(("partial", sleeve))
                if p["partial"]:
                    p["highest"] = max(p["highest"], close_px)
                    p["stop"] = R.trailing_stop(p["stop"], p["highest"], p["atr"])
            elif not paused and reg != "off":
                sig = I.entry(row)
                n_open = len(pos) + sum(1 for x in pending if x[0] == "entry")
                if sig and n_open < C.MAX_POSITIONS:
                    sym = cfg["x2"] if reg == "2x" and cfg["x2"] else cfg["x1"]
                    atr, px = data[sym].loc[t, "atr"], data[sym].loc[t, "close"]
                    committed = sum(x[3] for x in pending if x[0] == "entry")
                    value = R.position_value(equity, atr, px, max(cash - committed, 0))
                    if value >= 1:
                        pending.append(("entry", sleeve, sym, value, atr, sig))

    for sleeve in list(pos):
        sell(sleeve, pos[sleeve]["qty"], data[pos[sleeve]["symbol"]].loc[dates[-1], "close"], dates[-1], "end")
    return metrics(trades, curve, deposits, pauses)


def metrics(trades, curve, deposits, pauses) -> dict:
    eq = pd.DataFrame(curve, columns=["t", "equity", "dep", "spy"]).set_index("t")
    prev = eq["equity"].shift(1)
    r = ((eq["equity"] - eq["dep"] - prev) / prev).fillna(0)
    twr = (1 + r).cumprod()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    sp = ((eq["spy"] - eq["dep"] - eq["spy"].shift(1)) / eq["spy"].shift(1)).fillna(0)
    spy_twr = (1 + sp).cumprod()
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    deposited = sum(a for _, a in deposits)
    return {
        "start": str(eq.index[0])[:10], "end": str(eq.index[-1])[:10], "years": years,
        "deposited": deposited, "final_equity": float(eq["equity"].iloc[-1]),
        "cagr": float(twr.iloc[-1] ** (1 / years) - 1) if years > 0 else 0.0,
        "max_drawdown": float((twr / twr.cummax() - 1).min()),
        "sharpe": float(r.mean() / r.std() * math.sqrt(252)) if r.std() > 0 else 0.0,
        "trades": len(trades), "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf"),
        "avg_R": float(np.mean([t["R"] for t in trades])) if trades else 0.0,
        "exposure_2x": sum(1 for t in trades if t["symbol"] in C.LEVERAGED),
        "breaker_pauses": pauses,
        "by_symbol": {s: round(sum(t["pnl"] for t in trades if t["symbol"] == s), 2)
                      for s in sorted({t["symbol"] for t in trades})},
        "spy_final": float(eq["spy"].iloc[-1]),
        "spy_cagr": float(spy_twr.iloc[-1] ** (1 / years) - 1) if years > 0 else 0.0,
        "spy_max_drawdown": float((spy_twr / spy_twr.cummax() - 1).min()),
    }


def report(m: dict) -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return "\n".join([
        f"## Sean Realistisch backtest {m['start']} to {m['end']}",
        f"${C.START_CAPITAL:.0f} start + ${C.MONTHLY_DEPOSIT:.0f} on the 1st of each month; 0.05% slippage per fill",
        "", "| Metric | Sean Realistisch | Same deposits into SPY |", "|---|---|---|",
        f"| Deposited | ${m['deposited']:,.2f} | ${m['deposited']:,.2f} |",
        f"| Final equity | ${m['final_equity']:,.2f} | ${m['spy_final']:,.2f} |",
        f"| CAGR (time-weighted) | {m['cagr']:+.1%} | {m['spy_cagr']:+.1%} |",
        f"| Max drawdown | {m['max_drawdown']:.1%} | {m['spy_max_drawdown']:.1%} |",
        f"| Sharpe ratio | {m['sharpe']:.2f} | |",
        f"| Trades | {m['trades']} ({m['exposure_2x']} in 2x ETFs) | |",
        f"| Win rate | {m['win_rate']:.1%} | |",
        f"| Profit factor | {pf} | |",
        f"| Average result | {m['avg_R']:+.2f} R per trade | |",
        f"| Circuit-breaker pauses | {m['breaker_pauses']} | |",
        "", "P&L per ETF: " + ", ".join(f"{s} ${v:+.2f}" for s, v in m["by_symbol"].items())])


def main() -> None:
    if "--csv" in sys.argv:
        d = sys.argv[sys.argv.index("--csv") + 1]
        data = {f[:-4]: pd.read_csv(os.path.join(d, f), index_col=0, parse_dates=True)
                for f in os.listdir(d) if f.endswith(".csv")}
    else:
        data = download(C.BACKTEST_MONTHS)
    text = report(simulate(data))
    print(text)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report.md"), "w") as f:
        f.write(text + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
