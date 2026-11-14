"""Offline tests for the Market Making bot."""
import math
import random

from market_making import bot as B
from market_making.backtest import simulate
from market_making.quoting import MMParams, half_spread, needs_requote, quotes, sigma_from_closes

P = MMParams()


def test_spread_covers_fees():
    h = half_spread(0.0)                        # calm market: fee floor
    assert 2 * h >= 2 * P.maker_fee + P.min_edge - 1e-12
    assert half_spread(0.01) > h                # wild market: wider


def test_quotes_never_cross_and_skew_with_inventory():
    flat = quotes(100, 99.99, 100.01, 0.0005, 0, 500)
    assert flat["bid"] < 100 < flat["ask"] and flat["quote_bid"] and not flat["quote_ask"]
    full = quotes(100, 99.99, 100.01, 0.0005, 240, 500)
    assert full["bid"] < flat["bid"] and full["ask"] < flat["ask"]      # holding a lot: sell cheaper
    assert not full["quote_bid"] and full["quote_ask"]                   # inventory cap reached
    tight = quotes(100, 99.9, 99.95, 0.0, 0, 500)                        # weird book: clamp to best bid
    assert tight["bid"] <= 99.9


def test_sigma_and_requote():
    assert sigma_from_closes([100.0] * 10) == 0
    assert sigma_from_closes([100, 101, 100, 101, 100]) > 0
    assert needs_requote(None, 100) and needs_requote(100, 100.2) and not needs_requote(100, 100.05)


def test_ledger_round_trip_profit():
    o = lambda side, q, px: {"symbol": "BTC/USD", "side": side, "filled_qty": str(q), "filled_avg_price": str(px)}
    led = B.ledger([o("buy", 0.001, 100000), o("sell", 0.000998, 100500)])
    assert led["round_trips"] == 1 and led["realized"] > 0 and led["fees"] > 0
    assert round(led["cash"] - B.BUDGET, 4) == round(0.000998 * 100500 * (1 - B.FEE_COIN) - 100, 4)


def test_simulation_sideways_market_earns_spread():
    random.seed(1)
    bars, p = [], 100.0
    for i in range(3000):                       # mean-reverting chop around 100
        p += (100 - p) * 0.05 + random.gauss(0, 0.15)
        bars.append({"o": p, "h": p * 1.003, "l": p * 0.997, "c": p})
    r = simulate(bars, every=5)
    assert r["buys"] > 0 and r["sells"] > 0 and r["realized"] > 0


def test_simulation_crash_leaves_inventory():
    bars = [{"o": 100 - i * 0.01, "h": 100 - i * 0.01 + 0.02, "l": 100 - i * 0.01 - 0.4, "c": 100 - i * 0.01}
            for i in range(3000)]               # steady fall: bids keep filling, asks never
    r = simulate(bars, every=5)
    assert r["inventory_usd"] > 0 and r["pnl"] < 0


def test_run_once_places_bid_only_when_flat(tmp_path, monkeypatch):
    calls = []

    def fake_api(method, path, base=None, **kw):
        calls.append((method, path, kw.get("json")))
        return [] if method == "GET" else {}
    monkeypatch.setattr(B, "api", fake_api)
    monkeypatch.setattr(B, "book", lambda c: (100.0, 100.02))
    monkeypatch.setattr(B, "sigma_1m", lambda c: 0.0005)
    monkeypatch.setattr(B, "STATE_FILE", str(tmp_path / "s.json"))
    B.run_once()
    posts = [j for m, p, j in calls if m == "POST"]
    assert len(posts) == 2 and all(j["side"] == "buy" and j["type"] == "limit" for j in posts)   # BTC + ETH bids
    assert all(float(j["limit_price"]) < 100 for j in posts)
    assert all(j["client_order_id"].startswith("mm-B-") for j in posts)
