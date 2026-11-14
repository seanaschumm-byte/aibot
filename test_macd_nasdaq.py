"""Offline tests for MACD Nasdaq."""
import numpy as np
import pandas as pd

from macd_nasdaq.backtest import simulate
from macd_nasdaq.strategy import Rules, in_dip, lot_action, macd_state


def test_lot_rules():
    assert lot_action(100, 111.9, 111.9, False) == (None, 0.0)
    assert lot_action(100, 112, 112, False)[0] == "half"                 # +12%: half
    assert lot_action(100, 117, 117.9, True)[0] is None                  # never reached 18%: keep
    assert lot_action(100, 118.5, 120, True)[0] is None                  # above 18%: keep riding
    assert lot_action(100, 118, 121, True)[0] == "rest"                  # back to +18% after 21%: sell
    assert lot_action(100, 125, 125, True)[0] == "rest"                  # +25%: sell
    assert lot_action(100, 90, 105, False) == (None, 0.0)                # losing lot: only MACD exit


def test_signals():
    up = pd.Series(np.linspace(100, 130, 80))
    assert macd_state(up).iloc[-1]
    crash = pd.Series(list(np.linspace(100, 120, 60)) + list(np.linspace(120, 100, 10)))
    assert in_dip(crash).iloc[-1] and not in_dip(crash).iloc[50]       # 100/120 = -16.7%


def test_backtest_modes_and_deposits():
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2025-01-01", periods=500, tz="UTC")
    c = 400 * np.cumprod(1 + 0.0006 + rng.normal(0, 0.012, 500))
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c * 1.008, "low": c * 0.992, "close": c}, index=idx)
    res = {m: simulate(df, idx[250], m) for m in ("new", "macd", "dca")}
    assert len({round(r["deposited"]) for r in res.values()}) == 1          # same deposits everywhere
    assert all(-1 < r["dd"] <= 0 for r in res.values())
    assert res["new"]["buys"] > 0


def test_bot_day_cycle(tmp_path, monkeypatch):
    import datetime as dt
    from macd_nasdaq import bot as B
    monkeypatch.setattr(B, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(B, "notify", lambda t: None)
    monkeypatch.setattr(B, "eurusd", lambda: 1.1)
    monkeypatch.setattr(B, "api", lambda m, p, **k: {"is_open": True})
    px = {"v": 500.0}
    monkeypatch.setattr(B, "price", lambda tag: px["v"])
    monkeypatch.setattr(B, "all_cash", lambda fx, today: {t: 137.5 for t in B.MARKETS})
    orders = []
    monkeypatch.setattr(B, "order", lambda tag, side, usd=None, qty=None: orders.append((tag, side, usd, qty)) or
                        {"filled_qty": (usd / px["v"]) if usd else qty, "filled_avg_price": px["v"]})
    down_then_up = pd.Series(list(np.linspace(520, 480, 60)) + [470, 471, 474, 480, 490])   # MACD turns up at the end
    monkeypatch.setattr(B, "daily_closes", lambda tag: down_then_up)
    d1 = dt.datetime(2026, 11, 17, 15, 0, tzinfo=dt.timezone.utc)          # 10:00 New York
    s = B.run_once(d1)
    assert {o[0] for o in orders if o[1] == "buy"} == set(B.MARKETS) == {"CORE", "QQQ"}   # core + satellite
    assert all(abs(o[2] - 137.5 * 0.99) < 1e-6 for o in orders)
    B.run_once(d1 + dt.timedelta(minutes=30))                              # same day: no second buy
    assert len([o for o in orders if o[1] == "buy"]) == 2
    px["v"] = 560.0                                                        # +12%: satellite sells half,
    s = B.run_once(d1 + dt.timedelta(hours=1))                             # the core keeps everything
    sells = [o for o in orders if o[1] == "sell"]
    assert [o[0] for o in sells] == ["QQQ"] and s["QQQ"]["lots"][0]["half"] and not s["CORE"]["lots"][0].get("half")
    px["v"] = 805.0                                                        # +61%: variant A keeps everything
    s = B.run_once(d1 + dt.timedelta(hours=2))
    assert s["CORE"]["sold"] is None and s["CORE"]["lots"]
    px["v"] = 300.0                                                        # -40%: still never sells
    s = B.run_once(d1 + dt.timedelta(hours=2, minutes=30))
    assert s["CORE"]["lots"] and not [o for o in orders if o[0] == "CORE" and o[1] == "sell"]
    monkeypatch.setattr(B, "CORE_TAKE_PROFIT", 0.60)                       # optional rule still works
    px["v"] = 805.0
    s = B.run_once(d1 + dt.timedelta(hours=2, minutes=45))
    assert s["CORE"]["sold"] and not s["CORE"]["lots"]
    px["v"] = 1000.0                                                       # higher top after the sale
    B.run_once(d1 + dt.timedelta(hours=3))
    px["v"] = 705.0                                                        # 29.5% under 1000: not yet
    s = B.run_once(d1 + dt.timedelta(hours=4))
    assert s["CORE"]["sold"]
    px["v"] = 699.0                                                        # 30% under 1000: buy back
    s = B.run_once(d1 + dt.timedelta(hours=5))
    assert s["CORE"]["sold"] is None and s["CORE"]["lots"][0]["entry"] == 699.0


def test_botsi_uses_its_own_settings(tmp_path, monkeypatch):
    import datetime as dt
    import importlib
    import botsi.bot as botsi
    from macd_nasdaq import bot as E
    assert E.PREFIX == "bs" and set(E.MARKETS) == {"BTC", "ETH", "SOL"} and E.NAME == "Botsi" and E.PLAIN_MACD
    assert abs(sum(m[2] for m in E.MARKETS.values()) - 1) < 1e-9
    assert E.STATE_FILE.endswith("botsi/state/state.json")
    monkeypatch.setattr(E, "STATE_FILE", str(tmp_path / "b.json"))
    monkeypatch.setattr(E, "notify", lambda t: None)
    monkeypatch.setattr(E, "price", lambda tag: 100.0)
    monkeypatch.setattr(E, "all_cash", lambda fx, today: {t: 183.3 for t in E.MARKETS})
    orders = []
    monkeypatch.setattr(E, "order", lambda tag, side, usd=None, qty=None: orders.append((tag, side)) or
                        {"filled_qty": (usd / 100) if usd else qty, "filled_avg_price": 100.0})
    monkeypatch.setattr(E, "daily_closes", lambda tag: pd.Series(list(np.linspace(110, 95, 60)) + [94, 95, 97, 100, 103]))
    monkeypatch.setattr(E, "eurusd", lambda: 1.1)
    E.run_once(dt.datetime(2026, 11, 22, 3, 0, tzinfo=dt.timezone.utc))   # Sunday night: crypto still trades
    assert {o[0] for o in orders} == {"BTC", "ETH", "SOL"}
    monkeypatch.setattr(E, "price", lambda tag: 130.0)                     # +30%: plain MACD keeps holding
    E.run_once(dt.datetime(2026, 11, 22, 3, 30, tzinfo=dt.timezone.utc))
    assert all(o[1] == "buy" for o in orders)
    importlib.reload(E)                                                   # restore MACD Nasdaq settings


def test_core_sells_at_60_and_buys_back_at_minus_30():
    idx = pd.bdate_range("2025-01-01", periods=400, tz="UTC")
    flat = [100.0] * 100                                   # most money is invested around 100
    up = list(np.linspace(100, 250, 100))                  # far above +60% on the average cost
    down = list(np.linspace(250, 150, 200))                # then -40%: buy back at -30% from the top
    c = np.array(flat + up + down)
    df = pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999, "close": c}, index=idx)
    r = simulate(df, idx[0], "core", Rules(), 300, 60)
    assert any("sold 100%" in e for e in r["core_events"])
    assert any("bought back" in e for e in r["core_events"])


def test_core_variant_c_sells_a_quarter():
    idx = pd.bdate_range("2025-01-01", periods=400, tz="UTC")
    c = np.array([100.0] * 100 + list(np.linspace(100, 250, 100)) + list(np.linspace(250, 150, 200)))
    df = pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999, "close": c}, index=idx)
    full = simulate(df, idx[0], "core", Rules(), 300, 60, 0.0, 1.0)
    part = simulate(df, idx[0], "core", Rules(), 300, 60, 0.0, 0.25)
    never = simulate(df, idx[0], "core", Rules(), 300, 60, 0.0, 1.0, 99.0)
    assert any("sold 25%" in e for e in part["core_events"]) and any("bought back" in e for e in part["core_events"])
    assert not never["core_events"]
    assert len({round(x["deposited"]) for x in (full, part, never)}) == 1
