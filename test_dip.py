"""Offline tests for the crypto dip bot."""
import dip_bot
from dip_strategy import dip_signal


def make(dip=True, confirm=True, knife=False, depth=0.0, n=260):
    bars, p = [], 100.0
    for i in range(n):                                   # steady uptrend
        p *= 1.002 if i % 3 else 0.999
        bars.append({"o": p * 0.999, "h": p * 1.002, "l": p * 0.997, "c": p, "v": 100.0})
    from dip_strategy import rolling_vwap
    vw = rolling_vwap(bars, 24)
    bottom = vw * (1 - depth) * 1.002 if dip else p * 1.02    # no dip: price keeps rising
    closes = [p - (p - bottom) * f for f in (0.3, 0.6, 0.9, 1.0, 0.95)]
    for i, c in enumerate(closes):                      # red pullback bars
        o = bars[-1]["c"]
        lo = min(o, c) * (0.999 if i != 3 else 0.998)
        v = 600.0 if knife and i == 2 else 90.0
        bars.append({"o": o, "h": max(o, c) * 1.0005, "l": lo, "c": c if i != 4 else o * 1.0005, "v": v})
    o = bars[-1]["c"]
    if confirm:
        c = max(o, vw) * 1.008
        bars.append({"o": o, "h": c * 1.001, "l": o * 0.999, "c": c, "v": 150.0})
    else:
        bars.append({"o": o, "h": o * 1.001, "l": o * 0.995, "c": o * 0.996, "v": 80.0})
    return bars


def test_dip_signal():
    sig, why = dip_signal(make())
    assert sig, why


def test_no_dip():
    assert dip_signal(make(dip=False))[0] is None


def test_no_confirmation():
    sig, why = dip_signal(make(confirm=False))
    assert sig is None and "green" in why


def test_knife():
    sig, why = dip_signal(make(knife=True))
    assert sig is None and "knife" in why, why


def test_dump():
    sig, why = dip_signal(make(depth=0.05))
    assert sig is None and "dump" in why, why


def test_lots_pair_buy_and_sell():
    o = lambda cid, st, q="0", px=None, lp=None: {"client_order_id": cid, "symbol": "ETH/USD", "status": st,
        "submitted_at": "2026-11-14T10:00:00Z", "filled_qty": q, "filled_avg_price": px, "limit_price": lp}
    ls = dip_bot.lots([o("dip25-B-ETH-1", "filled", "0.01", "2000"),
                       o("dip25-S-ETH-1", "new", lp="2400"),
                       o("dip25-B-SOL-2", "filled", "1", "80")])
    assert ls[0]["sell"]["limit_price"] == "2400" and ls[1]["sell"] is None
