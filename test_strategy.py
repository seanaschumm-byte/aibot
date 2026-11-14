"""Offline tests: simulated 5-minute days, no network."""
from strategy import Params, evaluate, market_ok, position_size, session_vwap


def bar(o, h, l, c, v):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def uptrend_then_dip(confirm=True, knife=False, depth=0.0):
    """Morning rally 100 -> ~103, pullback toward VWAP, then a green reversal bar."""
    bars, p = [], 100.0
    for _ in range(8):                       # trend leg
        bars.append(bar(p, p + 0.5, p - 0.1, p + 0.4, 50_000))
        p += 0.4
    vw = session_vwap(bars)[-1]
    # pullback: three red bars drifting down to VWAP (optionally below it)
    path = [p - 0.6, (p + vw) / 2, vw * (1 - depth) + 0.05]
    if knife:                                # one huge red bar straight down to VWAP
        path = [p - 0.1, p - 0.2, vw + 0.05]
    for i, c in enumerate(path):
        o = bars[-1]["c"]
        v = 300_000 if knife and i == 2 else 25_000
        lo = c - 0.1
        bars.append(bar(o, o + 0.05, lo, c, v))
    o = bars[-1]["c"]
    if confirm:
        bars.append(bar(o, o + 0.65, o - 0.05, o + 0.6, 45_000))   # strong green, more volume
    else:
        bars.append(bar(o, o + 0.1, o - 0.3, o - 0.2, 20_000))     # another red bar
    return bars


AVG_DAILY = 1_000_000   # day so far ~600k in 60 min => rvol ~3.9


def run(bars, spy_chg=0.0):
    return evaluate("TEST", bars, AVG_DAILY, len(bars) * 5, spy_chg, Params())


def test_clean_pullback_gives_signal():
    sig, why = run(uptrend_then_dip())
    assert sig, why
    assert sig.stop < sig.entry < sig.target
    risk = sig.entry - sig.stop
    assert abs((sig.target - sig.entry) - 2 * risk) < 0.02          # 2:1 reward/risk
    assert risk / sig.entry <= 0.0201                               # stop within 2%


def test_no_confirmation_no_trade():
    sig, why = run(uptrend_then_dip(confirm=False))
    assert sig is None and "green" in why


def test_falling_knife_rejected():
    sig, why = run(uptrend_then_dip(knife=True))
    assert sig is None and "heavy red" in why, why


def test_deep_break_below_vwap_rejected():
    sig, why = run(uptrend_then_dip(depth=0.02))
    assert sig is None


def test_low_volume_rejected():
    sig, why = evaluate("TEST", uptrend_then_dip(), 50_000_000, 60, 0.0, Params())
    assert sig is None and "rvol" in why


def test_weaker_than_market_rejected():
    sig, why = run(uptrend_then_dip(), spy_chg=0.10)
    assert sig is None and "SPY" in why


def test_downtrend_rejected():
    bars = [bar(110 - i * 0.4, 110 - i * 0.4 + 0.1, 110 - i * 0.4 - 0.5, 110 - i * 0.4 - 0.4, 50_000)
            for i in range(12)]
    bars.append(bar(105.2, 105.9, 105.1, 105.8, 80_000))
    sig, _ = run(bars)
    assert sig is None


def test_market_filter():
    up = [bar(100 + i, 101 + i, 99.5 + i, 100.8 + i, 1000) for i in range(5)]
    assert market_ok(up)[0]
    down = [bar(100 - i, 100.2 - i, 98.5 - i, 98.8 - i, 1000) for i in range(5)]
    assert not market_ok(down)[0]


def test_position_size_respects_risk_and_cash():
    # EUR 75 ~ $86: 1% risk = $0.86; a 1% stop would want ~$86 of stock, capped by cash
    q = position_size(86.0, 86.0, 100.0, 99.0)
    assert q * 100 <= 86 * 0.98 + 1e-9
    assert q * (100 - 99) <= 0.86 + 1e-9
    # wide 2% stop -> half the size
    q2 = position_size(86.0, 86.0, 100.0, 98.0)
    assert abs(q2 * 2 - 0.86) < 0.01
    assert position_size(86, 86, 100, 101) == 0
