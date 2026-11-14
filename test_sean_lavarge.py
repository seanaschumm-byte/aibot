"""Offline tests for Sean Lavarge (no network, fake exchange)."""
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from sean_lavarge import config as C
from sean_lavarge import main as M
from sean_lavarge import risk_manager as R
from sean_lavarge.backtest import simulate
from sean_lavarge.strategy import add_indicators, entry_signal, exit_signal


def candles(n=260, drift=0.002, start=100.0, breakout=None, spike=3.0, seed=1):
    rng = np.random.default_rng(seed)
    t = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    c = start * np.cumprod(1 + drift + rng.normal(0, 0.004, n))
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c}, index=t)
    df["high"] = df[["open", "close"]].max(axis=1) * 1.003
    df["low"] = df[["open", "close"]].min(axis=1) * 0.997
    df["volume"] = 100.0
    if breakout:
        last = df.index[-1]
        hi = df["high"].iloc[-21:-1].max() if breakout == "up" else df["low"].iloc[-21:-1].min()
        px = hi * (1.02 if breakout == "up" else 0.98)
        df.loc[last, ["open", "close"]] = [df["close"].iloc[-2], px]
        df.loc[last, "high"] = max(px, df["close"].iloc[-2]) * 1.002
        df.loc[last, "low"] = min(px, df["close"].iloc[-2]) * 0.998
        df.loc[last, "volume"] = 100 * spike
    return df


def test_long_breakout_signal():
    row = add_indicators(candles(breakout="up")).iloc[-1]
    assert entry_signal(row) == "long"


def test_short_breakout_signal_in_downtrend():
    row = add_indicators(candles(drift=-0.002, breakout="down")).iloc[-1]
    assert entry_signal(row) == "short"


def test_no_volume_spike_no_trade():
    row = add_indicators(candles(breakout="up", spike=1.2)).iloc[-1]
    assert entry_signal(row) is None


def test_no_long_below_200_ema():
    df = candles(drift=-0.002)
    df = add_indicators(df)
    row = df.iloc[-1].copy()
    row["close"], row["volume"] = row["don_hi"] * 1.01, row["vol_ma"] * 3
    assert row["close"] < row["ema"] and entry_signal(row) != "long"


def test_turtle_exit():
    row = pd.Series({"close": 90.0, "exit_lo": 95.0, "exit_hi": 110.0})
    assert exit_signal(row, "long") and not exit_signal(row, "short")


def test_position_size_formula_and_cap():
    s = R.position_size(500, 1000, 60000)                 # wide ATR: formula wins
    assert abs(s.amount - 500 * 0.015 / 2000) < 1e-12 and not s.capped
    assert abs(s.risk - 7.5) < 1e-9                       # EUR 7.50 max loss on EUR 500
    s = R.position_size(500, 10, 60000)                   # tiny ATR: 10x cap kicks in
    assert s.capped and abs(s.notional - 5000) < 1e-6 and s.leverage == 10
    assert s.risk < 7.5                                   # capped size risks less than 1.5%
    assert R.leverage_for(1250, 500) == 3 and R.leverage_for(10, 500) == 1


def test_pyramid_only_for_winners():
    assert R.pyramid_due(100, 2, 103, "long", 0)          # +1.5 ATR
    assert not R.pyramid_due(100, 2, 102.9, "long", 0)
    assert not R.pyramid_due(100, 2, 95, "long", 0)       # never add to a loser
    assert R.pyramid_due(100, 2, 97, "short", 0)
    assert not R.pyramid_due(100, 2, 110, "long", 1)      # max 1 pyramid


def test_breakers():
    now = dt.datetime(2026, 12, 1, 12, tzinfo=dt.timezone.utc)
    assert R.breaker([], 1000, 954, now)[0]               # -4.6%
    assert not R.breaker([], 1000, 960, now)[0]
    losses = [{"closed": (now - dt.timedelta(hours=h)).isoformat(), "pnl": -5} for h in (10, 5, 1)]
    assert R.breaker(losses, None, 1000, now)[0]
    mixed = losses[:2] + [{"closed": now.isoformat(), "pnl": 3}]
    assert not R.breaker(mixed, None, 1000, now)[0]


def test_deposits_and_shield():
    now = dt.datetime(2026, 11, 17, 12, tzinfo=dt.timezone.utc)      # Tuesday after first Monday
    deps = R.scheduled_deposits(now)
    assert [a for _, a in deps] == [500, 150]
    assert R.locked_amount(deps, now) == 150                         # new EUR 150 not risked yet
    later = now + dt.timedelta(days=7)                               # next Tuesday
    assert R.locked_amount(R.scheduled_deposits(later), later) == 150   # previous unlocked, new one locked
    assert sum(a for _, a in R.scheduled_deposits(later)) == 800


def test_backtest_runs_and_reports():
    data = {"BTC/EUR": candles(1200, drift=0.001, seed=3), "ETH/EUR": candles(1200, drift=-0.0008, seed=4)}
    for df in data.values():                                         # random volume spikes
        df.loc[df.index[::17], "volume"] = 400
    m = simulate(data)
    assert m["trades"] > 0
    assert 0 <= m["win_rate"] <= 1 and m["max_drawdown"] <= 0
    assert m["deposited"] >= 500 and (m["deposited"] - 500) % 150 == 0
    assert set(m["by_symbol"]) <= {"BTC/EUR", "ETH/EUR"}


# ---------------------------------------------------------------- full cycle on a fake exchange
class FakeEx:
    def __init__(self, df):
        self.df, self.pos, self.orders, self.stops, self.px = df, None, [], [], float(df["close"].iloc[-1])

    def ohlcv(self, symbol, limit=300):
        return self.df

    def price(self, symbol):
        return self.px

    def equity(self):
        return 10000.0, 10000.0

    def position(self, symbol):
        return self.pos

    def setup_leverage(self, symbol, lev):
        self.lev = lev

    def min_amount(self, symbol):
        return 0.0

    def market(self, symbol, side, amount, reduce_only=False):
        self.orders.append((side, amount, reduce_only))
        if reduce_only:
            self.pos = None
        else:
            if self.pos:
                self.pos["amount"] += amount
            else:
                self.pos = {"side": "long" if side == "buy" else "short", "amount": amount,
                            "entry": self.px, "unrealized": 0.0}
        return {"average": self.px}

    def place_stop(self, symbol, side, amount, stop):
        self.stops = [(side, amount, stop)]

    def cancel_stops(self, symbol):
        self.stops = []

    def has_stop(self, symbol):
        return bool(self.stops)

    def realized_since(self, symbol, since):
        return None


@pytest.fixture
def one_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SYMBOLS", ["BTC/USD:USD"])
    monkeypatch.setattr(C, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(C, "CAPITAL_MODE", "virtual")
    monkeypatch.setattr(M.notifier, "send", lambda text: None)


def test_cycle_entry_pyramid_and_stop(one_symbol):
    now = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
    ex = FakeEx(candles(breakout="up"))
    s = M.run_once(ex, now)
    p = s["positions"]["BTC/USD:USD"]
    assert ex.orders[0][0] == "buy" and ex.stops and ex.stops[0][2] == pytest.approx(p["stop"])
    assert p["stop"] < p["first_entry"]
    # same candle again: no second entry
    ex2_orders = len(ex.orders)
    M.run_once(ex, now)
    assert len(ex.orders) == ex2_orders
    # price runs +1.5 ATR: pyramid + stop to breakeven
    ex.px = p["first_entry"] + 1.6 * p["atr"]
    s = M.run_once(ex, now + dt.timedelta(minutes=15))
    p = s["positions"]["BTC/USD:USD"]
    assert p["pyramids"] == 1 and p["stop"] == pytest.approx(p["first_entry"])
    assert ex.stops[0][1] == pytest.approx(p["amount"])
    # exchange stop fires: position gone -> trade recorded as a stop at breakeven
    ex.pos = None
    s = M.run_once(ex, now + dt.timedelta(minutes=30))
    assert not s["positions"] and s["closed"][-1]["reason"] == "stop"
    assert json.load(open(C.STATE_FILE))["closed"]
