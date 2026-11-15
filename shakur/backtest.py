"""Replay AI trading bot Shakur (all 6 sleeves together) over several periods.
  python -m shakur.backtest
"""
from __future__ import annotations

import os

import pandas as pd

from justin.backtest import load, simulate
from macd_nasdaq.backtest import combine
from .config import CRYPTO, CRYPTO_FEE, SHARE, SLEEVES, STOCKS, START_EUR, WEEKLY_EUR


def run(months: float, stocks: dict, crypto: dict) -> tuple[dict, dict]:
    end = min(min(d.index[-1] for d in stocks.values()), min(d.index[-1] for d in crypto.values()))
    start = end - pd.Timedelta(days=int(months * 30.44))
    per, curves, dep = {}, [], 0.0
    for tag, (name, universe, max_pos, add) in SLEEVES.items():
        src = crypto if "/" in universe[0] else stocks
        data = {s: src[s].loc[:end] for s in universe if s in src}
        r = simulate(data, name, start, max_pos, CRYPTO_FEE if "/" in universe[0] else 0.0,
                     START_EUR * SHARE, WEEKLY_EUR * SHARE, add)
        per[tag] = r
        curves.append(r["curve"])
        dep += r["deposited"]
    tot = combine(curves)
    tot["deposited"] = dep
    trades = [x for r in per.values() for x in r["trade_results"]]
    tot["trades"], tot["win"] = len(trades), (sum(x > 0 for x in trades) / len(trades) if trades else 0.0)
    return tot, per


def main() -> None:
    stocks, crypto = load(STOCKS, 24), load(CRYPTO, 24)
    lines = ["## AI trading bot Shakur: 6 strategies, equal shares, EUR 500 + EUR 100/week", "",
             "| Period | Deposited | End value | **Profit** | Return (excl. deposits) | Max drawdown | Trades | Win rate |",
             "|---|---|---|---|---|---|---|---|"]
    detail = []
    for months in (6, 12, 24):
        tot, per = run(months, stocks, crypto)
        lines.append(f"| last {months} months | EUR {tot['deposited']:,.0f} | EUR {tot['final']:,.0f} | "
                     f"**EUR {tot['final'] - tot['deposited']:+,.0f}** | {tot['ret']:+.1%} | {tot['dd']:.1%} | "
                     f"{tot['trades']} | {tot['win']:.0%} |")
        detail.append(f"{months}m per sleeve: " + ", ".join(
            f"{t} EUR {r['final'] - r['deposited']:+,.0f}" for t, r in per.items()))
    text = "\n".join(lines + [""] + detail)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
