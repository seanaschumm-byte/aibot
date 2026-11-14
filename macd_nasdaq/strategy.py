"""MACD Nasdaq: rules as pure functions (no network).

Buy:  MACD(12,26,9) crosses above its signal line -> invest all cash in QQQ; after that every
      weekly deposit while MACD stays above.
      Dip: QQQ closes >= 12% under its highest close of the last 3 months (63 trading days) ->
      invest all cash, and every weekly deposit while the dip lasts (also when MACD is below).
      Money from sales goes into the NEXT buy (weekly buy, MACD buy signal or dip). After a MACD
      sell signal it waits in cash until MACD turns up again or a dip starts.
Sell, per purchase ("lot"):
      +12%  -> sell 50% of the lot
      +25%  -> sell the rest
      once the lot has been >= +18% and falls back to +18% -> sell the rest (locks in 18%)
      MACD crosses below its signal line -> sell everything that is left.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Rules:
    fast: int = 12
    slow: int = 26
    signal: int = 9
    dip: float = 0.12
    dip_lookback: int = 63          # ~3 months of trading days
    tp1: float = 0.12               # sell half
    tp1_fraction: float = 0.5
    tp2: float = 0.25               # sell the rest
    floor: float = 0.18             # ... or here, once the lot has been at/above it


def macd_state(close: pd.Series, r: Rules = Rules()) -> pd.Series:
    """True where the MACD line is above its signal line."""
    m = close.ewm(span=r.fast, adjust=False).mean() - close.ewm(span=r.slow, adjust=False).mean()
    return m > m.ewm(span=r.signal, adjust=False).mean()


def in_dip(close: pd.Series, r: Rules = Rules()) -> pd.Series:
    """True where the close is >= 12% under the highest close of the last ~3 months."""
    high = close.rolling(r.dip_lookback, min_periods=20).max()
    return close <= high * (1 - r.dip)


def lot_action(entry: float, price: float, peak: float, half_sold: bool, r: Rules = Rules()) -> tuple[str | None, float]:
    """What to do with one lot at `price` (peak = highest price seen since the lot was bought).
    Returns (action, sell_price): 'half' = sell 50% at +12%, 'rest' = sell what is left."""
    gain, peak_gain = price / entry - 1, peak / entry - 1
    if not half_sold and gain >= r.tp1:
        return "half", entry * (1 + r.tp1)
    if half_sold or peak_gain >= r.tp1:
        if gain >= r.tp2:
            return "rest", entry * (1 + r.tp2)
        if peak_gain >= r.floor and gain <= r.floor:
            return "rest", entry * (1 + r.floor)
    return None, 0.0
