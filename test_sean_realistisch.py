"""Offline tests for Sean Realistisch (no network, fake broker)."""
import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from sean_realistisch import config as C
from sean_realistisch import indicators as I
from sean_realistisch import main as M
from sean_realistisch import risk_manager as R
from sean_realistisch.backtest import simulate


def daily(n=320, drift=0.0008, start=100.0, seed=1, vol=1e6):
    rng = np.random.default_rng(seed)
    t = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    c = start * np.cumprod(1 + drift + rng.normal(0, 0.008, n))
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c, "volume": vol}, index=t)
    df["high"] = df[["open", "close"]].max(axis=1) * 1.004
    df["low"] = df[["open", "close"]].min(axis=1) * 0.996
    return df


def test_regimes():
    up = I.add(daily(drift=0.002))
    assert I.regime(up.iloc[-1], up.iloc[-2]) in ("2x", "1x")
    down = I.add(daily(drift=-0.002))
    assert I.regime(down.iloc[-1], down.iloc[-2]) == "off"
    row = pd.Series({"close": 105, "sma50": 110, "sma200": 100, "ema20": 106})
    assert I.regime(row, pd.Series({"ema20": 107})) == "1x"          # between 200 and 50 SMA
    row = pd.Series({"close": 115, "sma50": 110, "sma200": 100, "ema20": 112})
    assert I.regime(row, pd.Series({"ema20": 111})) == "2x"          # above both, EMA20 rising
    assert I.regime(row, pd.Series({"ema20": 113})) == "1x"          # EMA20 falling


def test_entries():
    base = {"sma200": 100.0, "high20": 130.0, "vol20": 1e6}
    assert I.entry(pd.Series({**base, "close": 110, "rsi2": 12, "volume": 1e6})) == "pullback"
    assert I.entry(pd.Series({**base, "close": 95, "rsi2": 12, "volume": 1e6})) is None     # below 200 SMA
    assert I.entry(pd.Series({**base, "close": 130, "rsi2": 70, "volume": 1.4e6})) == "breakout"
    assert I.entry(pd.Series({**base, "close": 130, "rsi2": 70, "volume": 1.2e6})) is None   # weak volume


def test_rsi2_range():
    r = I.rsi(daily()["close"], 2)
    assert r.between(0, 100).all()


def test_position_value_formula_and_caps():
    # $500, ATR $2 on a $100 ETF: 2% risk / 4% stop = $250 -> under the 40% cap ($200)? no: capped
    assert R.position_value(500, 2, 100, 500) == pytest.approx(200)          # 40% cap
    assert R.position_value(500, 5, 100, 500) == pytest.approx(100)          # 10/(10/100)
    assert R.position_value(500, 5, 100, 60) == pytest.approx(60)           # cash cap (no margin)


def test_take_profit_and_trailing():
    assert R.take_profit_due(100, 2, 101, 90, False).startswith("RSI")
    assert R.take_profit_due(100, 2, 110.1, 50, False).startswith("profit")   # 2.5 * 4 = +10
    assert R.take_profit_due(100, 2, 110.1, 90, True) is None
    assert R.trailing_stop(96, 120, 2) == 115 and R.trailing_stop(118, 120, 2) == 118
    assert R.pyramid_due(100, 2, 103, False) and not R.pyramid_due(100, 2, 102.9, False)


def test_deposits_monthly():
    d = R.scheduled_deposits(dt.date(2027, 2, 15))
    assert [a for _, a in d] == [500, 300, 300, 300]                          # Dec 1, Jan 1, Feb 1
    assert d[1][0] == dt.date(2026, 12, 1)


def test_breaker():
    assert R.breaker([("d1", 1000), ("d2", 1010)], 945)[0]                   # -6.4%
    assert not R.breaker([("d1", 1000)], 950)[0]


def test_ledger_own_orders_only():
    o = lambda side, q, px, cid: {"symbol": "SPY", "side": side, "qty": q, "price": px, "cid": cid, "time": "t"}
    led = M.ledger([o("buy", 1, 100, "sr-B"), o("sell", 0.5, 110, "sr-X"), o("sell", 0.5, 90, "sr-S")], 500)
    assert led["cash"] == pytest.approx(500 - 100 + 55 + 45) and "SPY" not in led["qty"]
    assert led["closes"][-1]["full"] and led["closes"][0]["pnl"] == pytest.approx(5)


def test_backtest_runs():
    data = {"SPY": daily(800, 0.0006, 400, 1), "QQQ": daily(800, 0.0008, 350, 2),
            "SSO": daily(800, 0.0012, 80, 3), "QLD": daily(800, 0.0016, 90, 4), "GLD": daily(800, 0.0003, 180, 5),
            "TLT": daily(800, -0.0002, 90, 6), "IBIT": daily(800, 0.0015, 40, 7)}
    for d in data.values():
        d.loc[d.index[::13], "volume"] = 2e6
    m = simulate(data)
    assert m["trades"] > 0 and m["deposited"] >= 500 and (m["deposited"] - 500) % 300 == 0
    assert -1 <= m["max_drawdown"] <= 0 and 0 <= m["win_rate"] <= 1


# ---------------------------------------------------------------- full cycle with a fake broker
class FakeBroker:
    def __init__(self, bars):
        self.bars, self.orders, self.stops, self.px, self.n = bars, [], {}, {}, 0

    def market_open_now(self):
        return True, "open"

    def account_equity(self):
        return 100000.0, 100000.0

    def cash_deposits_since(self, since):
        return []

    def daily_bars(self, symbols, days=420):
        return {s: self.bars[s] for s in symbols}

    def price(self, s):
        return self.px.get(s, float(self.bars[s]["close"].iloc[-1]))

    def _order(self, sym, side, qty, price, cid):
        self.n += 1
        o = SimpleNamespace(id=self.n, symbol=sym, side=side, filled_qty=qty, filled_avg_price=price,
                            client_order_id=cid, filled_at="2026-11-20T15:00:00Z", submitted_at="x", status="filled")
        self.orders.append(o)
        return o

    def my_orders(self, since):
        return list(self.orders)

    def buy_notional(self, sym, usd, cid):
        return self._order(sym, "buy", usd / self.price(sym), self.price(sym), cid)

    def sell_qty(self, sym, qty, cid):
        return self._order(sym, "sell", qty, self.price(sym), cid)

    def wait_filled(self, oid, secs=30):
        return next(o for o in self.orders if o.id == oid)

    def open_stops(self, sym):
        s = self.stops.get(sym)
        return [SimpleNamespace(stop_price=s[1], qty=s[0], id=0)] if s else []

    def place_stop(self, sym, qty, stop, cid):
        self.stops[sym] = (qty, round(stop, 2))

    def cancel_stops(self, sym):
        self.stops.pop(sym, None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(C, "CAPITAL_MODE", "virtual")
    monkeypatch.setattr(M.notifier, "send", lambda text: None)


def test_cycle_buy_tp_and_stop(setup):
    bars = {s: daily(320, 0.002, 100, i) for i, s in enumerate(["SPY", "QQQ", "SSO", "QLD", "GLD", "TLT", "IBIT"])}
    spy = bars["SPY"]
    spy.loc[spy.index[-1], "close"] = spy["close"].iloc[-2] * 0.97        # sharp dip -> RSI(2) low
    spy.loc[spy.index[-1], "low"] = spy["close"].iloc[-1] * 0.995
    for s in ("QQQ", "GLD", "TLT", "IBIT"):                                # no signal there
        bars[s]["close"] = np.linspace(200, 100, 320)
    b = FakeBroker(bars)
    now = dt.datetime(2026, 11, 20, 15, tzinfo=dt.timezone.utc)
    s = M.run_once(b, now)
    assert "SPY" in s["positions"]
    p = s["positions"]["SPY"]
    sym = p["symbol"]
    assert sym in ("SPY", "SSO") and b.stops[sym][1] == pytest.approx(round(p["stop"], 2))
    assert p["first_value"] <= 0.40 * 500 + 1e-6                           # 40% cap on $500
    # next run: RSI(2) spike -> sell 50%
    idx = bars["SPY"]
    idx.loc[idx.index[-1], "close"] = idx["close"].iloc[-2] * 1.03
    s = M.run_once(b, now + dt.timedelta(minutes=30))
    assert s["positions"]["SPY"]["partial"]
    assert b.stops[sym][0] == pytest.approx(M.ledger([M.order_dict(o) for o in b.orders], 500)["qty"][sym], rel=1e-4)
    # stop fills on Alpaca -> position recorded as closed
    left = M.ledger([M.order_dict(o) for o in b.orders], 500)["qty"][sym]
    b._order(sym, "sell", left, b.price(sym), "sr-S-x")
    s = M.run_once(b, now + dt.timedelta(minutes=60))
    assert "SPY" not in s["positions"] and s["closed"][-1]["symbol"] == sym
