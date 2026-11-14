"""Backtest Sean Lavarge on historical 4h candles with EUR 500 start + EUR 150 every Monday.

  python -m sean_lavarge.backtest              download 12 months (Coinbase public data, EUR pairs)
  python -m sean_lavarge.backtest --csv DIR    use DIR/<BTC-EUR>.csv files (ts,open,high,low,close,volume)

Rules are the same as the live bot (strategy.py + risk_manager.py). Fills: entries/exits on the
next candle's open, stops intrabar (at the stop, or at the open if price gapped through it),
taker fee + slippage on every fill, perp funding while a position is open, isolated-margin
liquidation caps the loss of one position at its margin.
Outputs Win Rate, Profit Factor, Max Drawdown and Sharpe Ratio (on time-weighted returns, so the
weekly deposits don't count as profit).
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys

import numpy as np
import pandas as pd

from . import config as C
from . import risk_manager as R
from .strategy import add_indicators, entry_signal, exit_signal


# ---------------------------------------------------------------- data
def download(symbol: str, days: int) -> pd.DataFrame:
    import ccxt
    ex = getattr(ccxt, C.BACKTEST_EXCHANGE)({"enableRateLimit": True})
    since = ex.milliseconds() - days * 86400_000
    rows = []
    while True:
        batch = ex.fetch_ohlcv(symbol, "1h", since, 300)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 3600_000
        if since > ex.milliseconds() - 3600_000:
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).drop_duplicates("ts")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return to_4h(df.set_index("ts").sort_index())


def to_4h(df: pd.DataFrame) -> pd.DataFrame:
    out = df.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna()


# ---------------------------------------------------------------- simulation
def simulate(data: dict[str, pd.DataFrame], start_cash: float = C.START_CAPITAL_EUR,
             weekly: float = C.WEEKLY_DEPOSIT_EUR) -> dict:
    data = {s: add_indicators(df) for s, df in data.items()}
    index = sorted(set().union(*[df.index for df in data.values()]))
    warm = C.EMA_TREND + 1
    t0 = max(df.index[min(warm, len(df) - 1)] for df in data.values())
    index = [t for t in index if t >= t0]
    fee = C.TAKER_FEE + C.SLIPPAGE
    funding_per_bar = C.FUNDING_PER_8H * (4 / 8)

    cash, deposits, positions, trades, curve = start_cash, [(index[0], start_cash)], {}, [], []
    pending: dict[str, tuple] = {}
    paused_until, pauses, last_monday = None, 0, None
    recent: list[dict] = []

    def locked(t):
        return sum(a for i, (d, a) in enumerate(deposits) if i > 0 and t - d < pd.Timedelta(days=C.DEPOSIT_LOCK_DAYS))

    def unreal(t):
        u = 0.0
        for s, p in positions.items():
            if t in data[s].index:
                p["mark"] = data[s].loc[t, "close"]
            d = 1 if p["side"] == "long" else -1
            u += (p["mark"] - p["avg"]) * p["amount"] * d
        return u

    def close(s, px, t, reason):
        nonlocal cash
        p = positions.pop(s)
        d = 1 if p["side"] == "long" else -1
        exit_px = px * (1 - d * C.SLIPPAGE)
        pnl = (exit_px - p["avg"]) * p["amount"] * d - C.TAKER_FEE * p["amount"] * exit_px - p["costs"]
        pnl = max(pnl, -p["margin"])                     # isolated: liquidation caps the loss
        cash += pnl
        trades.append({"symbol": s, "side": p["side"], "opened": p["opened"], "closed": t, "pnl": pnl,
                       "reason": reason, "pyramids": p["pyramids"], "R": pnl / p["risk"] if p["risk"] else 0})
        recent.append({"closed": t.isoformat(), "pnl": pnl})

    for t in index:
        # weekly deposit, Monday 00:00 UTC
        monday = (t - pd.Timedelta(days=t.weekday())).normalize()
        if t.weekday() == 0 and t.hour == 0 and monday != last_monday and t > index[0]:
            cash += weekly
            deposits.append((t, weekly))
            last_monday = monday
        dep_t = weekly if deposits[-1][0] == t else 0.0

        for s, df in data.items():
            if t not in df.index:
                continue
            bar = df.loc[t]
            # 1. orders decided at the previous close fill at this open
            if s in pending:
                kind, *args = pending.pop(s)
                if kind == "exit" and s in positions:
                    close(s, bar["open"], t, "turtle 10-bar exit")
                elif kind == "entry" and s not in positions:
                    side, amount, atr, lev, risk = args
                    d = 1 if side == "long" else -1
                    px = bar["open"] * (1 + d * C.SLIPPAGE)
                    positions[s] = {"side": side, "first_entry": px, "avg": px, "amount": amount,
                                    "first_amount": amount, "atr": atr, "stop": R.stop_price(px, atr, side),
                                    "pyramids": 0, "opened": t, "mark": px, "risk": risk,
                                    "margin": amount * px / lev, "lev": lev,
                                    "costs": C.TAKER_FEE * amount * px}
            if s not in positions:
                continue
            p = positions[s]
            d = 1 if p["side"] == "long" else -1
            # 2. hard stop intrabar (conservative: checked before the pyramid)
            hit = bar["low"] <= p["stop"] if d == 1 else bar["high"] >= p["stop"]
            if hit:
                gap = bar["open"] < p["stop"] if d == 1 else bar["open"] > p["stop"]
                close(s, bar["open"] if gap else p["stop"], t, "stop")
                continue
            # 3. pyramid at +1.5 ATR: add 50%, stop to breakeven
            trigger = p["first_entry"] + d * C.PYRAMID_TRIGGER_ATR * p["atr"]
            reached = bar["high"] >= trigger if d == 1 else bar["low"] <= trigger
            if reached and p["pyramids"] < C.MAX_PYRAMIDS and not (paused_until and t < paused_until):
                add = p["first_amount"] * C.PYRAMID_FRACTION
                eq = cash + unreal(t)
                if (p["amount"] + add) * trigger <= (eq - locked(t)) * C.MAX_LEVERAGE:
                    px = trigger * (1 + d * C.SLIPPAGE)
                    p["avg"] = (p["avg"] * p["amount"] + px * add) / (p["amount"] + add)
                    p["amount"] += add
                    p["margin"] += add * px / p["lev"]
                    p["costs"] += C.TAKER_FEE * add * px
                    p["pyramids"] += 1
                    p["stop"] = p["first_entry"]
            # 4. funding while held
            p["costs"] += funding_per_bar * p["amount"] * bar["close"]

        equity = cash + unreal(t)
        curve.append((t, equity, dep_t))

        # 5. circuit breakers
        now = t.to_pydatetime()
        if not (paused_until and t < paused_until):
            past = [e for (tt, e, _) in curve if tt >= t - pd.Timedelta(hours=24)]
            hit, _ = R.breaker(recent, past[0] if past else None, equity, now)
            if hit:
                paused_until, pauses = t + pd.Timedelta(hours=C.PAUSE_HOURS), pauses + 1
        paused = bool(paused_until and t < paused_until)

        # 6. signals at this close -> orders at the next open
        risk_eq = equity - locked(t)
        used = sum(p["amount"] * p["mark"] / p["lev"] for p in positions.values())
        for s, df in data.items():
            if t not in df.index:
                continue
            row = df.loc[t]
            if s in positions:
                if exit_signal(row, positions[s]["side"]):
                    pending[s] = ("exit",)
            elif not paused and len(positions) + len([1 for v in pending.values() if v[0] == "entry"]) < C.MAX_OPEN_POSITIONS:
                sig = entry_signal(row)
                if sig:
                    size = R.position_size(risk_eq, row["atr"], row["close"], max(risk_eq - used, 0))
                    if size.amount * row["close"] >= 10:          # exchange minimum ~ EUR 10
                        lev = R.leverage_for(size.notional, risk_eq)
                        pending[s] = ("entry", sig, size.amount, row["atr"], lev, size.risk)
                        used += size.notional / lev

    # close what's still open at the last price
    for s in list(positions):
        close(s, positions[s]["mark"], index[-1], "end of test")
    if curve:
        curve[-1] = (curve[-1][0], cash, curve[-1][2])
    return metrics(trades, curve, deposits, pauses)


def metrics(trades, curve, deposits, pauses) -> dict:
    eq = pd.DataFrame(curve, columns=["t", "equity", "deposit"]).set_index("t")
    prev = eq["equity"].shift(1)
    r = ((eq["equity"] - eq["deposit"] - prev) / prev).fillna(0)           # time-weighted returns
    twr = (1 + r).cumprod()
    dd = (twr / twr.cummax() - 1).min()
    daily = (1 + r).groupby(eq.index.date).prod() - 1
    sharpe = daily.mean() / daily.std() * math.sqrt(365) if daily.std() > 0 else 0.0
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    deposited = sum(a for _, a in deposits)
    final = float(eq["equity"].iloc[-1]) if len(eq) else 0.0
    return {"trades": len(trades), "win_rate": len(wins) / len(trades) if trades else 0.0,
            "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf"),
            "max_drawdown": float(dd), "sharpe": float(sharpe), "twr_return": float(twr.iloc[-1] - 1),
            "deposited": deposited, "final_equity": final, "net_profit": final - deposited,
            "avg_R": float(np.mean([t["R"] for t in trades])) if trades else 0.0,
            "pyramids": sum(t["pyramids"] for t in trades), "breaker_pauses": pauses,
            "by_symbol": {s: round(sum(t["pnl"] for t in trades if t["symbol"] == s), 2)
                          for s in sorted({t["symbol"] for t in trades})},
            "stops": sum(t["reason"] == "stop" for t in trades),
            "start": str(eq.index[0])[:10] if len(eq) else "", "end": str(eq.index[-1])[:10] if len(eq) else "",
            "trade_list": trades}


def report(m: dict) -> str:
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    lines = [f"## Sean Lavarge backtest {m['start']} to {m['end']}",
             f"EUR {C.START_CAPITAL_EUR:.0f} start + EUR {C.WEEKLY_DEPOSIT_EUR:.0f}/week, max {C.MAX_LEVERAGE}x isolated, "
             f"risk {C.RISK_PER_TRADE:.1%}/trade, fees+slippage+funding included",
             "", "| Metric | Value |", "|---|---|",
             f"| Deposited | EUR {m['deposited']:,.2f} |",
             f"| Final equity | EUR {m['final_equity']:,.2f} |",
             f"| Net profit (after deposits) | EUR {m['net_profit']:+,.2f} |",
             f"| Time-weighted return | {m['twr_return']:+.1%} |",
             f"| Trades | {m['trades']} (stopped out: {m['stops']}, pyramids added: {m['pyramids']}) |",
             f"| Win rate | {m['win_rate']:.1%} |",
             f"| Profit factor | {pf} |",
             f"| Average result per trade | {m['avg_R']:+.2f} R |",
             f"| Max drawdown | {m['max_drawdown']:.1%} |",
             f"| Sharpe ratio (annualised, daily) | {m['sharpe']:.2f} |",
             f"| Circuit-breaker pauses | {m['breaker_pauses']} |",
             "", "P&L per market: " + ", ".join(f"{s} EUR {v:+.2f}" for s, v in m["by_symbol"].items())]
    return "\n".join(lines)


def main() -> None:
    if "--csv" in sys.argv:
        d = sys.argv[sys.argv.index("--csv") + 1]
        data = {f.replace(".csv", ""): to_4h(pd.read_csv(os.path.join(d, f), index_col=0, parse_dates=True))
                for f in os.listdir(d) if f.endswith(".csv")}
    else:
        days = C.BACKTEST_DAYS + 40       # + warm-up for the 200 EMA
        data = {}
        for s in C.BACKTEST_SYMBOLS:
            print(f"downloading {s} ...", flush=True)
            data[s] = download(s, days)
    m = simulate(data)
    text = report(m)
    print(text)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_report.md")
    with open(out, "w") as f:
        f.write(text + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
