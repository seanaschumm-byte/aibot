"""VWAP pullback strategy: pure functions, no network. Easy to test.

Each rule notes which trader's published teaching it comes from (see README).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Params:
    min_bars: int = 6                 # need ~30 min of 5-min bars before judging a trend
    min_rvol: float = 1.5             # Cameron / Aziz: "stocks in play" trade on heavy relative volume
    trend_above_frac: float = 0.6     # Shannon: most of the session spent above VWAP = buyers in control
    vwap_rise_lookback: int = 6       # Shannon: VWAP itself must be rising (30 min on 5-min bars)
    pullback_lookback: int = 4        # Raschke: first pullback, recent bars only
    touch_above: float = 0.003        # pullback low within 0.3% above VWAP counts as a test
    max_below: float = 0.01           # more than 1% under VWAP = dumping, not dipping
    knife_vol_mult: float = 2.0       # falling-knife filter: huge red volume bars
    knife_range_mult: float = 2.0
    max_extension: float = 0.01       # don't chase: entry must be within 1% of VWAP
    min_stop_pct: float = 0.003
    max_stop_pct: float = 0.02        # user rule: stop 1-2% below VWAP / under pullback low
    reward_r: float = 2.0             # Cameron / user: at least 2:1 reward to risk


@dataclass
class Signal:
    symbol: str
    entry: float
    stop: float
    target: float
    rvol: float
    rel_strength: float
    notes: list[str] = field(default_factory=list)


def session_vwap(bars: list[dict]) -> list[float]:
    """Cumulative VWAP per bar. Uses each bar's own vwap ('vw') when present."""
    out, pv, vol = [], 0.0, 0.0
    for b in bars:
        price = b.get("vw") or (b["h"] + b["l"] + b["c"]) / 3
        pv += price * b["v"]
        vol += b["v"]
        out.append(pv / vol if vol else b["c"])
    return out


def ema(values: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def market_ok(spy_bars: list[dict]) -> tuple[bool, str]:
    """Livermore: trade with the general market. SPY must be above its session VWAP."""
    if len(spy_bars) < 2:
        return False, "not enough SPY bars"
    vw = session_vwap(spy_bars)[-1]
    last = spy_bars[-1]["c"]
    return last > vw, f"SPY {last:.2f} vs VWAP {vw:.2f}"


def day_change(bars: list[dict]) -> float:
    return bars[-1]["c"] / bars[0]["o"] - 1 if bars else 0.0


def evaluate(symbol: str, bars: list[dict], avg_daily_vol: float, minutes_elapsed: float,
             spy_change: float, p: Params = Params()) -> tuple[Signal | None, str]:
    """Return (signal, reason). bars = today's COMPLETED 5-min bars, oldest first."""
    if len(bars) < p.min_bars:
        return None, "too few bars"
    vwap = session_vwap(bars)
    closes = [b["c"] for b in bars]
    last, prev = bars[-1], bars[-2]

    # Stocks in play (Cameron, Aziz): relative volume vs. normal pace for this time of day
    day_vol = sum(b["v"] for b in bars)
    expected = avg_daily_vol * min(1.0, max(minutes_elapsed, 1) / 390)
    rvol = day_vol / expected if expected else 0.0
    if rvol < p.min_rvol:
        return None, f"rvol {rvol:.2f} < {p.min_rvol}"

    # Leaders only (Kullamaggie): stronger than the market today
    rs = day_change(bars) - spy_change
    if rs <= 0:
        return None, f"weaker than SPY ({rs:+.2%})"

    # Established uptrend (Shannon): mostly above a rising VWAP
    above = sum(c > v for c, v in zip(closes[:-1], vwap[:-1])) / (len(bars) - 1)
    if above < p.trend_above_frac:
        return None, f"only {above:.0%} of bars above VWAP"
    lb = min(p.vwap_rise_lookback, len(vwap) - 1)
    if vwap[-1] <= vwap[-1 - lb]:
        return None, "VWAP not rising"

    # Trend on fast EMAs (Raschke)
    if ema(closes, 9)[-1] <= ema(closes, 20)[-1]:
        return None, "EMA9 <= EMA20"

    # The dip: recent bars (before the signal bar) tested VWAP
    pb = bars[-1 - p.pullback_lookback:-1]
    pb_vwap = vwap[-1 - p.pullback_lookback:-1]
    pb_low = min(b["l"] for b in pb)
    ref_vwap = min(pb_vwap)
    if pb_low > ref_vwap * (1 + p.touch_above):
        return None, "no pullback to VWAP"
    if pb_low < ref_vwap * (1 - p.max_below):
        return None, "broke >1% below VWAP (dump, not dip)"

    # Never catch a falling knife: no big-volume, wide-range red bars in the pullback
    avg_vol = day_vol / len(bars)
    avg_rng = sum(b["h"] - b["l"] for b in bars) / len(bars)
    for b in pb:
        if b["c"] < b["o"] and b["v"] > p.knife_vol_mult * avg_vol and \
                (b["h"] - b["l"]) > p.knife_range_mult * avg_rng:
            return None, "heavy red selling in pullback"

    # Confirmation (Brooks / user): green signal bar, closes in its upper half,
    # back above VWAP, higher close than the previous bar, on rising volume
    rng = last["h"] - last["l"]
    if not (last["c"] > last["o"]):
        return None, "signal bar not green"
    if rng <= 0 or (last["c"] - last["l"]) / rng < 0.5:
        return None, "signal bar closed weak"
    if last["c"] <= vwap[-1]:
        return None, "not back above VWAP"
    if last["c"] <= prev["c"] or last["v"] <= prev["v"]:
        return None, "no rising price+volume"

    entry = last["c"]
    if entry > vwap[-1] * (1 + p.max_extension):
        return None, "too extended above VWAP"

    # Stop just under the pullback low, kept within 0.3%-2% of entry
    stop = min(pb_low, last["l"]) * 0.999
    stop = min(stop, entry * (1 - p.min_stop_pct))
    stop = max(stop, entry * (1 - p.max_stop_pct))
    risk = entry - stop
    target = entry + p.reward_r * risk

    return Signal(symbol, round(entry, 2), round(stop, 2), round(target, 2), rvol, rs,
                  [f"rvol {rvol:.1f}", f"RS {rs:+.2%}", f"VWAP {vwap[-1]:.2f}",
                   f"pullback low {pb_low:.2f}"]), "signal"


def position_size(equity: float, cash: float, entry: float, stop: float,
                  risk_pct: float = 0.01) -> float:
    """Brandt: risk ~1% of capital per trade. Never use more than available cash (no margin)."""
    risk = entry - stop
    if risk <= 0 or entry <= 0:
        return 0.0
    qty = min(equity * risk_pct / risk, cash * 0.98 / entry)
    return max(0.0, int(qty * 10000) / 10000)  # fractional shares, 4 decimals
