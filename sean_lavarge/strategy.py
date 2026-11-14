"""Signals: 4h 200-EMA trend filter + 20-bar Donchian breakout with a volume spike (Turtles),
exit on the opposite 10-bar channel. Works on a pandas OHLCV DataFrame of CLOSED candles."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema"] = df["close"].ewm(span=C.EMA_TREND, adjust=False).mean()
    # channels of the PREVIOUS n bars (the breakout bar itself must exceed them)
    df["don_hi"] = df["high"].rolling(C.DONCHIAN_ENTRY).max().shift(1)
    df["don_lo"] = df["low"].rolling(C.DONCHIAN_ENTRY).min().shift(1)
    df["exit_hi"] = df["high"].rolling(C.DONCHIAN_EXIT).max().shift(1)
    df["exit_lo"] = df["low"].rolling(C.DONCHIAN_EXIT).min().shift(1)
    df["vol_ma"] = df["volume"].rolling(C.VOLUME_MA).mean().shift(1)
    prev = df["close"].shift(1)
    tr = np.maximum(df["high"] - df["low"],
                    np.maximum((df["high"] - prev).abs(), (df["low"] - prev).abs()))
    df["atr"] = tr.ewm(alpha=1 / C.ATR_PERIOD, adjust=False).mean()   # Wilder ATR
    return df


def entry_signal(row: pd.Series) -> str | None:
    """'long', 'short' or None for one indicator row (a closed candle)."""
    if any(pd.isna(row[k]) for k in ("ema", "don_hi", "don_lo", "vol_ma", "atr")):
        return None
    spike = row["volume"] > C.VOLUME_SPIKE * row["vol_ma"]
    if not spike:
        return None
    if row["close"] > row["ema"] and row["close"] > row["don_hi"]:
        return "long"
    if C.ALLOW_SHORTS and row["close"] < row["ema"] and row["close"] < row["don_lo"]:
        return "short"
    return None


def exit_signal(row: pd.Series, side: str) -> bool:
    """Turtle exit: a long closes below the 10-bar low, a short above the 10-bar high."""
    if side == "long":
        return not pd.isna(row["exit_lo"]) and row["close"] < row["exit_lo"]
    return not pd.isna(row["exit_hi"]) and row["close"] > row["exit_hi"]


def explain(row: pd.Series) -> str:
    return (f"close {row['close']:.2f} | EMA200 {row['ema']:.2f} | 20-bar high {row['don_hi']:.2f} "
            f"low {row['don_lo']:.2f} | vol {row['volume'] / row['vol_ma']:.1f}x avg | ATR {row['atr']:.2f}")
