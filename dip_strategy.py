"""Crypto 'buy the dip in an uptrend' on 1-hour bars: the same idea as the stock VWAP
pullback bot (strategy.py), adapted for 24/7 crypto using a rolling 24-hour VWAP."""
from __future__ import annotations

from dataclasses import dataclass

from crypto_strategy import ema


@dataclass
class DipParams:
    vwap_hours: int = 24          # rolling VWAP window (crypto has no session open)
    trend_ema: int = 200          # PTJ / Livermore: only buy in an uptrend
    fast_ema: int = 20            # Raschke: fast EMA above slow EMA
    slow_ema: int = 50
    pullback_bars: int = 6        # the dip happened in the last 6 hours
    touch_above: float = 0.005    # low within 0.5% above VWAP counts as a test
    max_below: float = 0.03       # >3% under VWAP = dumping, not dipping
    knife_vol_mult: float = 2.5   # no huge red volume bars in the dip
    max_extension: float = 0.02   # don't chase: entry within 2% of VWAP


def rolling_vwap(bars: list[dict], n: int) -> float:
    w = bars[-n:]
    vol = sum(b["v"] for b in w)
    if not vol:
        return w[-1]["c"]
    return sum((b.get("vw") or (b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in w) / vol


def dip_signal(bars: list[dict], p: DipParams = DipParams()) -> tuple[dict | None, str]:
    """bars: completed 1h bars, oldest first. Returns (signal, reason)."""
    if len(bars) < p.trend_ema + p.vwap_hours:
        return None, "not enough history"
    closes = [b["c"] for b in bars]
    last, prev = bars[-1], bars[-2]
    if last["c"] <= ema(closes, p.trend_ema)[-1]:
        return None, "below 200 EMA (no uptrend)"
    if ema(closes, p.fast_ema)[-1] <= ema(closes, p.slow_ema)[-1]:
        return None, "EMA20 <= EMA50"

    vwap = rolling_vwap(bars, p.vwap_hours)
    dip = bars[-1 - p.pullback_bars:-1]
    low = min(b["l"] for b in dip)
    if low > vwap * (1 + p.touch_above):
        return None, "no dip to VWAP"
    if low < vwap * (1 - p.max_below):
        return None, "fell >3% under VWAP (dump, not dip)"
    avg_vol = sum(b["v"] for b in bars[-p.vwap_hours:]) / p.vwap_hours
    if any(b["c"] < b["o"] and b["v"] > p.knife_vol_mult * avg_vol for b in dip):
        return None, "heavy red selling in dip (falling knife)"

    rng = last["h"] - last["l"]
    if not (last["c"] > last["o"]) or rng <= 0 or (last["c"] - last["l"]) / rng < 0.5:
        return None, "no strong green confirmation bar"
    if last["c"] <= vwap:
        return None, "not back above VWAP"
    if last["c"] <= prev["c"] or last["v"] <= prev["v"]:
        return None, "no rising price + volume"
    if last["c"] > vwap * (1 + p.max_extension):
        return None, "too far above VWAP (chasing)"

    change_24h = last["c"] / bars[-1 - p.vwap_hours]["c"] - 1
    return {"entry": last["c"], "vwap": vwap, "dip_low": low, "change_24h": change_24h}, "signal"
