"""Daily indicators, regime and entry signals (pandas)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def add(df: pd.DataFrame) -> pd.DataFrame:
    """df: daily bars with open/high/low/close/volume, oldest first."""
    df = df.copy()
    c = df["close"]
    df["sma50"] = c.rolling(C.SMA_FAST).mean()
    df["sma200"] = c.rolling(C.SMA_SLOW).mean()
    df["ema20"] = c.ewm(span=C.EMA_SHORT, adjust=False).mean()
    df["rsi2"] = rsi(c, C.RSI_PERIOD)
    prev = c.shift(1)
    tr = np.maximum(df["high"] - df["low"], np.maximum((df["high"] - prev).abs(), (df["low"] - prev).abs()))
    df["atr"] = tr.ewm(alpha=1 / C.ATR_PERIOD, adjust=False).mean()
    df["high20"] = c.rolling(C.BREAKOUT_DAYS).max()                 # incl. today: "closes at a 20-day high"
    df["vol20"] = df["volume"].rolling(C.BREAKOUT_DAYS).mean().shift(1)
    return df


def regime(row: pd.Series, prev: pd.Series) -> str:
    """'2x', '1x' or 'off' for the index (PTJ 200-day rule + Seykota golden cross)."""
    if any(pd.isna(row[k]) for k in ("sma50", "sma200", "ema20")):
        return "off"
    golden = row["close"] > row["sma200"] and row["sma50"] > row["sma200"]
    if not golden:
        return "off"
    if row["close"] > row["sma50"] and row["ema20"] > prev["ema20"]:
        return "2x"
    return "1x"


def entry(row: pd.Series) -> str | None:
    """'pullback' (Connors RSI-2), 'breakout' (Dennis 20-day high + volume) or None."""
    if pd.isna(row["sma200"]):
        return None
    if row["rsi2"] < C.RSI_ENTRY and row["close"] > row["sma200"]:
        return "pullback"
    if row["close"] >= row["high20"] and not pd.isna(row["vol20"]) and row["volume"] >= C.BREAKOUT_VOLUME * row["vol20"]:
        return "breakout"
    return None
