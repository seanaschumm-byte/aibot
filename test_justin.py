"""Offline tests for Justin (Bollinger Band bounce)."""
import datetime as dt

import numpy as np
import pandas as pd

from justin import bot as J
from justin.strategies import STRATEGIES, indicators


def bars(dip: bool, n=260, seed=0, recover=False):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + 0.0005 + rng.normal(0, 0.005, n))
    if dip:
        c[-1] = c[-2] * 0.93                                # sharp drop: close under the lower band
    if recover:
        c[-1] = c[-2] * 1.05                                # clear bounce: above the 20-day average
    t = pd.bdate_range("2025-11-01", periods=n, tz="UTC")
    return indicators(pd.DataFrame({"open": c, "high": c * 1.003, "low": c * 0.997, "close": c, "volume": 1e6}, index=t))


def test_bollinger_rules():
    s = STRATEGIES["Bollinger Band bounce"]
    d = bars(True).iloc[-1]
    assert s["entry"](d)
    assert not s["entry"](bars(False).iloc[-1])
    assert s["exit"]({"close": 101, "bb_mid": 100}, {"days": 2}) and s["exit"]({"close": 90, "bb_mid": 100}, {"days": 30})
    assert not s["exit"]({"close": 95, "bb_mid": 100}, {"days": 5})


def test_day_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(J, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(J, "notify", lambda t: None)
    monkeypatch.setattr(J.journal, "write", lambda *a, **k: None)
    monkeypatch.setattr(J, "eurusd", lambda: 1.1)
    monkeypatch.setattr(J, "api", lambda m, p, **k: {"is_open": True})
    data = {s: bars(i < 7, seed=i) for i, s in enumerate(J.UNIVERSE)}        # 7 stocks dip today
    monkeypatch.setattr(J, "daily_bars", lambda: data)
    held, orders = {}, []
    monkeypatch.setattr(J, "ledger", lambda fx, today: (550.0 - sum(v * 100 for v in held.values()), dict(held)))

    def fake_order(sym, side, usd=None, qty=None):
        orders.append((sym, side, usd))
        if side == "buy":
            held[sym] = usd / 100
        else:
            held.pop(sym, None)
        return {"filled_qty": (usd / 100) if usd else qty, "filled_avg_price": 100.0}
    monkeypatch.setattr(J, "order", fake_order)
    now = dt.datetime(2026, 11, 17, 15, 0, tzinfo=dt.timezone.utc)          # 10:00 New York
    s = J.run_once(now)
    buys = [o for o in orders if o[1] == "buy"]
    assert len(buys) == 5 and len(s["positions"]) == 5                       # max 5 of the 7 dips
    assert all(o[2] <= 550 / 5 for o in buys)                                # each <= 1/5 of equity
    J.run_once(now + dt.timedelta(minutes=30))                               # once per day
    assert len(orders) == 5
    first = set(held)
    for sym in first:                                                        # next day: prices recovered
        data[sym] = bars(False, seed=J.UNIVERSE.index(sym), recover=True)
    s = J.run_once(now + dt.timedelta(days=1))
    assert {o[0] for o in orders if o[1] == "sell"} == first                 # all 5 sold at the middle band
    still_dipping = set(J.UNIVERSE[:7]) - first
    assert set(s["positions"]) and set(s["positions"]) <= still_dipping      # freed slots go to the other dips
