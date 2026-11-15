"""Offline tests for AI trading bot Shakur."""
import datetime as dt

import numpy as np
import pandas as pd

from shakur import bot as S
from shakur.config import SHARE, SLEEVES


def series(kind, n=260, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + 0.0015 + rng.normal(0, 0.004, n))           # steady uptrend
    if kind == "dip":
        c[-1] = c[-2] * 0.90                                               # deep 1-day dip, still above SMA200
    if kind == "breakout":
        c[-1] = c[-2] * 1.06
    t = pd.bdate_range("2025-11-01", periods=n, tz="UTC")
    from justin.strategies import indicators
    return indicators(pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1e6}, index=t))


def test_shares_and_sleeves():
    assert abs(SHARE * len(SLEEVES) - 1) < 1e-9 and len(SLEEVES) == 6
    assert S.deposits_usd(dt.date(2026, 11, 16), 1.0) == 500 / 6
    assert abs(S.deposits_usd(dt.date(2026, 11, 30), 1.0) - 700 / 6) < 1e-9


def test_full_day(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(S, "notify", lambda t: None)
    monkeypatch.setattr(S.journal, "write", lambda *a, **k: None)
    monkeypatch.setattr(S, "eurusd", lambda: 1.1)
    monkeypatch.setattr(S, "api", lambda m, p, **k: {"is_open": True})
    stocks = {s: series("dip" if s in ("SPY", "AAPL", "NVDA") else "flat", seed=i) for i, s in enumerate(S.STOCKS)}
    crypto = {"BTC/USD": series("breakout", seed=50), "ETH/USD": series("dip", seed=51), "SOL/USD": series("flat", seed=52)}
    monkeypatch.setattr(S, "daily_bars", lambda syms, c: crypto if c else stocks)
    cash = 550 / 6
    monkeypatch.setattr(S, "ledgers", lambda fx, today: {t: (cash, {}) for t in SLEEVES})
    orders = []
    monkeypatch.setattr(S, "order", lambda tag, sym, side, usd=None, qty=None: orders.append((tag, sym, side, usd)) or
                        {"filled_qty": (usd / 100) if usd else qty, "filled_avg_price": 100.0})
    s = S.run_once(dt.datetime(2026, 11, 17, 15, 0, tzinfo=dt.timezone.utc))
    by = {}
    for tag, sym, side, usd in orders:
        by.setdefault(tag, []).append(sym)
    assert by["TURTLE"] == ["BTC/USD"]                                     # 20-day breakout
    assert "ETH/USD" in by["CONN_CRYPTO"]                                  # RSI(2) dip in an uptrend
    assert "SPY" in by["CONN_SPY"] and "SPY" in by["FABER"]
    assert set(by["BOLL"]) <= {"SPY", "AAPL", "NVDA"}                      # only the dips
    assert all(usd <= cash for *_, usd in orders)                          # never more than the sleeve's cash
    n = len(orders)
    S.run_once(dt.datetime(2026, 11, 17, 15, 30, tzinfo=dt.timezone.utc))  # same day: nothing new
    assert len(orders) == n
