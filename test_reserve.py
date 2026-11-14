"""Offline tests for the Smart Reserve bot."""
import datetime as dt
import reserve_bot as rb
from reserve_strategy import rsi, check_buy, check_sell


def h1_dip(top=100.0, now=88.0, green=True):
    """7 days around `top`, then a slide to `now`."""
    bars = [{"o": top, "h": top, "l": top * 0.99, "c": top * 0.995, "t": f"h{i}"} for i in range(170)]
    p = top
    for i in range(10):
        p -= (top - now) / 10
        bars.append({"o": p + 0.5, "h": p + 0.6, "l": p - 0.2, "c": p, "t": f"d{i}"})
    bars[-1] = {"o": now - 0.5, "h": now + 0.2, "l": now - 0.6, "c": now, "t": "last"} if green else bars[-1]
    return bars


def h4_down(n=40):
    return [{"c": 100 - i * 1.0} for i in range(n)]        # steady fall -> very low RSI


M15 = [{"c": 87.0}] * 9


def test_rsi_extremes():
    assert rsi([float(i) for i in range(30)]) == 100.0
    assert rsi([float(30 - i) for i in range(30)]) < 5
    assert 40 < rsi([100, 101, 100, 101] * 10) < 60


def test_alt_buy_ok():
    ok, why, rs = check_buy("SOL/USD", 88.0, h1_dip(), h4_down(), M15, 0, 600, 28, None, None)
    assert ok, why


def test_dip_too_small_for_alt_but_ok_for_btc():
    ok, why, _ = check_buy("SOL/USD", 93.0, h1_dip(now=93.0), h4_down(), M15, 0, 600, 28, None, None)
    assert not ok and "under 7d high" in why
    ok, why, _ = check_buy("BTC/USD", 93.0, h1_dip(now=93.0), h4_down(), M15, 0, 600, 28, None, None)
    assert ok, why


def test_rsi_not_low_enough():
    flat = [{"c": 100 + (i % 2)} for i in range(40)]
    ok, why, _ = check_buy("SOL/USD", 88.0, h1_dip(), flat, M15, 0, 600, 28, None, None)
    assert not ok and "RSI" in why


def test_free_fall_blocked():
    h1 = h1_dip(green=False)                                 # last hour red
    ok, why, _ = check_buy("SOL/USD", 88.0, h1, h4_down(), [{"c": 95.0}] * 9, 0, 600, 28, None, None)
    assert not ok and "falling" in why


def test_cap_cooldown_and_rebuy_gap():
    ok, why, _ = check_buy("SOL/USD", 88.0, h1_dip(), h4_down(), M15, 40, 600, 28, None, None)
    assert not ok and "cap" in why                           # (40+28)/600 > 10%
    ok, why, _ = check_buy("SOL/USD", 88.0, h1_dip(), h4_down(), M15, 0, 600, 28, 20, None)
    assert not ok and "cooldown" in why
    ok, why, _ = check_buy("SOL/USD", 88.0, h1_dip(), h4_down(), M15, 0, 600, 28, 60, 90.0)
    assert not ok and "5%" in why                            # 88 is only 2.2% under 90
    ok, why, _ = check_buy("SOL/USD", 88.0, h1_dip(), h4_down(), M15, 0, 600, 28, 60, 95.0)
    assert ok, why


def test_trailing_take_profit():
    bars = [{"h": 105}, {"h": 113}]                          # alt target = +12% of 100 = 112
    assert check_sell("SOL/USD", 100, 112.5, bars)[0] is False   # riding: stop at 113*0.975=110.2
    assert check_sell("SOL/USD", 100, 110.0, bars)[0] is True
    assert check_sell("SOL/USD", 100, 108.0, [{"h": 110}])[0] is False  # target never reached
    assert check_sell("ETH/USD", 100, 108.0, [{"h": 109}])[0] is False   # 109*0.985=107.4
    assert check_sell("ETH/USD", 100, 107.3, [{"h": 109}])[0] is True


def test_deposits_and_ledger():
    assert rb.deposits_eur(dt.date(2026, 11, 14)) == 500
    assert rb.deposits_eur(dt.date(2026, 11, 16)) == 600
    assert rb.deposits_eur(dt.date(2026, 11, 30)) == 800
    o = lambda side, q, px, cid: {"side": side, "filled_qty": q, "filled_avg_price": px, "symbol": "SOL/USD",
                                  "client_order_id": cid, "submitted_at": "2026-11-14T10:00:00Z"}
    led = rb.ledger([o("buy", "1", "100", "rsv-B-SOL-1"), o("buy", "1", "90", "rsv-B-SOL-2")],
                    dt.date(2026, 11, 14), 1.0)
    sol = led["coins"]["SOL/USD"]
    assert round(sol["qty"], 4) == 1.995 and sol["cost"] == 190 and sol["last_buy_price"] == 90
    assert round(led["cash"], 2) == 310.0
    led = rb.ledger([o("buy", "1", "100", "b"), o("sell", "0.9975", "120", "s")], dt.date(2026, 11, 14), 1.0)
    assert led["coins"]["SOL/USD"]["qty"] == 0 and len(led["closes"]) == 1
    assert round(led["closes"][0]["pnl_usd"], 2) == round(0.9975 * 120 * 0.9975 - 100, 2)
