"""Buy-the-dip / sell-high strategies for Justin Tradingbot (pure functions on daily bars).

Every strategy answers two questions per symbol on a completed daily candle:
  entry(row)            -> buy tomorrow?
  exit(row, pos)        -> sell tomorrow?   (pos: entry price, days held)
`rank` decides which dips get the free slots first (lower = more oversold = first).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["close"]
    d = c.diff()

    def rsi(n):
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(100)

    df["rsi2"], df["rsi14"] = rsi(2), rsi(14)
    df["sma5"], df["sma20"], df["sma200"] = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(200).mean()
    sd = c.rolling(20).std()
    df["bb_low"], df["bb_mid"] = df["sma20"] - 2 * sd, df["sma20"]
    df["high20"] = c.rolling(20).max()
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["ibs"] = ((c - df["low"]) / rng).fillna(0.5)                     # internal bar strength
    df["prev_high"] = df["high"].shift(1)
    df["down3"] = (d < 0) & (d.shift(1) < 0) & (d.shift(2) < 0)
    df["high20_prev"] = df["high"].rolling(20).max().shift(1)          # Turtle: breakout over the PREVIOUS 20 days
    df["low10_prev"] = df["low"].rolling(10).min().shift(1)
    return df


def up(row) -> bool:
    return not pd.isna(row["sma200"]) and row["close"] > row["sma200"]


STRATEGIES = {
    "Connors RSI(2)": {                       # Larry Connors: short dips in a long uptrend
        "entry": lambda r: up(r) and r["rsi2"] < 10,
        "exit": lambda r, p: r["close"] > r["sma5"],
        "rank": lambda r: r["rsi2"]},
    "RSI(14) 30/70": {                        # classic oversold / overbought
        "entry": lambda r: r["rsi14"] < 30,
        "exit": lambda r, p: r["rsi14"] > 70 or p["days"] >= 40,
        "rank": lambda r: r["rsi14"]},
    "Bollinger Band bounce": {                # close under the lower band, sell at the middle
        "entry": lambda r: r["close"] < r["bb_low"],
        "exit": lambda r, p: r["close"] >= r["bb_mid"] or p["days"] >= 30,
        "rank": lambda r: r["close"] / r["bb_low"]},
    "8% dip, sell +8%": {                     # 8% under the 20-day high in an uptrend; +8% target, -8% stop
        "entry": lambda r: up(r) and r["close"] <= 0.92 * r["high20"],
        "exit": lambda r, p: r["close"] >= p["entry"] * 1.08 or r["close"] <= p["entry"] * 0.92 or p["days"] >= 30,
        "rank": lambda r: r["close"] / r["high20"]},
    "IBS mean reversion": {                   # close near the day's low in an uptrend; sell above yesterday's high
        "entry": lambda r: up(r) and r["ibs"] < 0.2,
        "exit": lambda r, p: r["close"] > r["prev_high"] or p["days"] >= 10,
        "rank": lambda r: r["ibs"]},
    "3 down days": {                          # three lower closes in a row in an uptrend; sell above the 5-day SMA
        "entry": lambda r: up(r) and bool(r["down3"]),
        "exit": lambda r, p: r["close"] > r["sma5"] or p["days"] >= 10,
        "rank": lambda r: r["rsi2"]},
    "Connors RSI(2), sell at RSI > 70": {     # Connors entry; sell when RSI(2) > 70 (a "sell high" exit)
        "entry": lambda r: up(r) and r["rsi2"] < 10,
        "exit": lambda r, p: r["rsi2"] > 70 or p["days"] >= 15,
        "rank": lambda r: r["rsi2"]},
}

# Trend strategies (used by Shakur). Not dip strategies, so not in Justin's dip test.
TREND_STRATEGIES = {
    "Turtle breakout": {                      # Richard Dennis: buy a 20-day high, sell a 10-day low
        "entry": lambda r: not pd.isna(r["high20_prev"]) and r["close"] > r["high20_prev"],
        "exit": lambda r, p: r["close"] < r["low10_prev"],
        "rank": lambda r: -r["close"] / r["high20_prev"]},
    "Faber 200-day trend": {                  # Meb Faber: hold while above the 200-day average, else cash
        "entry": lambda r: up(r),
        "exit": lambda r, p: not up(r),
        "rank": lambda r: 0.0},
}
ALL_STRATEGIES = {**STRATEGIES, **TREND_STRATEGIES}
