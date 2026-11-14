"""Aggressive BTC trend/breakout strategy on 1-hour bars. Pure functions, no network.

Rules merged from famous high-risk / big-winner traders (see README, "BTC bot").
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CParams:
    trend_ema: int = 200          # Paul Tudor Jones: nothing good happens below the 200 MA
    fast_ema: int = 20            # Ed Seykota: trade with the trend (fast EMA above slow)
    slow_ema: int = 50
    breakout_n: int = 20          # Richard Dennis (Turtles) / Nicolas Darvas: new 20-bar high
    vol_mult: float = 1.5         # Dan Zanger: breakouts must come on heavy volume
    range_atr: float = 1.0        # Larry Williams: volatility expansion on the breakout bar
    atr_n: int = 14
    stop_atr: float = 2.0         # Turtles: initial stop 2N (2x ATR) below entry
    trail_atr: float = 3.0        # Seykota / Kullamaggie: wide trailing stop, let winners run
    take_profit: float = 5.0      # user cap: +500% on a trade = sell


def ema(values: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def atr(bars: list[dict], n: int) -> float:
    trs = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["o"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    recent = trs[-n:]
    return sum(recent) / len(recent)


def entry_signal(bars: list[dict], p: CParams = CParams()) -> tuple[dict | None, str]:
    """bars = completed 1h bars, oldest first (need >= trend_ema + 1)."""
    if len(bars) < p.trend_ema + 1:
        return None, f"need {p.trend_ema + 1} bars, have {len(bars)}"
    closes = [b["c"] for b in bars]
    last = bars[-1]
    if last["c"] <= ema(closes, p.trend_ema)[-1]:
        return None, "below 200 EMA (PTJ trend filter)"
    if ema(closes, p.fast_ema)[-1] <= ema(closes, p.slow_ema)[-1]:
        return None, "EMA20 <= EMA50 (no trend)"
    prior = bars[-1 - p.breakout_n:-1]
    level = max(b["h"] for b in prior)
    if last["c"] <= level:
        return None, f"no breakout (close {last['c']:.0f} <= 20-bar high {level:.0f})"
    avg_vol = sum(b["v"] for b in prior) / len(prior)
    if last["v"] < p.vol_mult * avg_vol:
        return None, "breakout on weak volume"
    a = atr(bars, p.atr_n)
    if last["h"] - last["l"] < p.range_atr * a:
        return None, "no volatility expansion"
    entry = last["c"]
    return {"entry": entry, "stop": entry - p.stop_atr * a, "atr": a,
            "breakout_level": level}, "signal"


def trailing_stop(bars_since_entry: list[dict], entry: float, current_stop: float | None,
                  a: float, p: CParams = CParams()) -> float:
    """Stop only ever moves up: max(initial 2-ATR stop, highest close - 3 ATR, existing stop)."""
    best = max([b["c"] for b in bars_since_entry] or [entry])
    s = max(entry - p.stop_atr * a, best - p.trail_atr * a)
    return max(s, current_stop or 0.0)
