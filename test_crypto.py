"""Offline tests for the BTC breakout strategy (simulated hourly bars)."""
import crypto_bot
from crypto_strategy import CParams, entry_signal, trailing_stop, atr


def bars_uptrend_then_breakout(breakout=True, vol=300.0, n=260):
    bars, p = [], 60_000.0
    for i in range(n):                      # steady uptrend with small chop
        p *= 1.0015 if i % 3 else 0.999
        bars.append({"o": p * 0.999, "h": p * 1.003, "l": p * 0.996, "c": p, "v": 100.0})
    for _ in range(20):                     # sideways box (Darvas)
        bars.append({"o": p, "h": p * 1.004, "l": p * 0.996, "c": p * 1.001, "v": 100.0})
    top = p * 1.004
    c = top * 1.02 if breakout else top * 0.999
    bars.append({"o": p, "h": c * 1.002, "l": p * 0.998, "c": c, "v": vol})
    return bars


def test_breakout_signal():
    sig, why = entry_signal(bars_uptrend_then_breakout())
    assert sig, why
    assert sig["stop"] < sig["entry"]
    assert abs((sig["entry"] - sig["stop"]) - 2 * sig["atr"]) < 1e-6


def test_no_breakout():
    sig, why = entry_signal(bars_uptrend_then_breakout(breakout=False))
    assert sig is None and "no breakout" in why


def test_weak_volume_rejected():
    sig, why = entry_signal(bars_uptrend_then_breakout(vol=110))
    assert sig is None and "volume" in why


def test_downtrend_rejected():
    bars = [{"o": 90_000 - i * 50, "h": 90_000 - i * 50 + 80, "l": 90_000 - i * 50 - 80,
             "c": 90_000 - i * 50 - 40, "v": 100} for i in range(260)]
    bars[-1] = dict(bars[-1], c=bars[-2]["h"] + 500, h=bars[-2]["h"] + 600, v=500)
    sig, why = entry_signal(bars)
    assert sig is None


def test_trailing_stop_only_moves_up():
    bars = [{"o": 100, "h": 101, "l": 99, "c": c, "v": 1} for c in (100, 110, 120, 105)]
    s1 = trailing_stop(bars[:1], 100, None, 2.0)
    assert s1 == 96                       # entry - 2 ATR
    s2 = trailing_stop(bars[:3], 100, s1, 2.0)
    assert s2 == 114                      # best close 120 - 3 ATR
    s3 = trailing_stop(bars, 100, s2, 2.0)
    assert s3 == 114                      # pullback to 105 does not lower it


def test_ledger_counts_trades():
    def o(side, q, px, t):
        return {"side": side, "filled_qty": q, "filled_avg_price": px, "filled_at": t,
                "submitted_at": t, "client_order_id": "btcbot-x"}
    orders = [o("buy", "0.001", "100000", "2026-11-14T10:00Z"),
              o("sell", "0.000998", "110000", "2026-11-15T10:00Z"),   # fee dust left
              o("buy", "0.001", "105000", "2026-11-16T10:00Z")]
    led = crypto_bot.ledger(orders)
    assert len(led["closed"]) == 1 and led["open"]["entry"] == 105000
    assert round(led["closed"][0]["pnl"], 2) == round(0.000998 * 110000 - 100, 2)
