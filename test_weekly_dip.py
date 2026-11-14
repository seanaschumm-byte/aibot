"""Offline tests for the weekly dip bot."""
import datetime as dt
import weekly_dip_bot as w
from weekly_dip_strategy import plan_orders, weekly_vwap, crashed, fmt_price


def test_weekly_vwap_weights_volume():
    bars = [{"h": 10, "l": 10, "c": 10, "v": 1}, {"h": 20, "l": 20, "c": 20, "v": 3}]
    assert weekly_vwap(bars) == 17.5


def test_plan_levels_and_budget():
    plan = plan_orders({"ETH/USD": 2000.0, "SOL/USD": 100.0}, 200.0)
    assert len(plan) == 6
    assert abs(sum(p["usd"] for p in plan) - 200.0) < 1e-6          # never more than the budget
    eth = [p for p in plan if p["coin"] == "ETH/USD"]
    assert [round(p["limit"]) for p in eth] == [1800, 1700, 1600]    # -10/-15/-20%
    assert eth[2]["usd"] > eth[0]["usd"]                              # more money deeper


def test_crash_filter():
    falling = [{"c": 100 * (0.999 ** i)} for i in range(750)]        # ~ -53% over 30 days
    flat = [{"c": 100.0} for _ in range(750)]
    assert crashed(falling) and not crashed(flat)


def test_deposits_per_iso_week():
    assert w.deposits(dt.date(2026, 11, 14), 1.0) == (1, 175.0)      # start week
    assert w.deposits(dt.date(2026, 11, 16), 1.0) == (2, 350.0)      # next Monday
    assert w.deposits(dt.date(2027, 1, 30), 1.0) == (5, 875.0)       # capped at 5 weeks
    assert w.week_id(dt.date(2026, 11, 14)) == "2026W46"


def test_ledger_cash_and_lots():
    o = lambda cid, side, q, px, st="filled": {"client_order_id": cid, "side": side, "filled_qty": q,
        "filled_avg_price": px, "status": st, "symbol": "SOL/USD", "submitted_at": "2026-11-15T01:00Z"}
    orders = [o("dip10-B-SOL-2026W46-L1-1", "buy", "1", "90"),
              o("dip10-S-SOL-2026W46-L1-1", "sell", "0.9975", "100.8"),
              o("dip10-B-SOL-2026W46-L2-1", "buy", "0.5", "85", st="canceled"),   # partial fill
              o("dip10-B-SOL-2026W46-L3-1", "buy", "0", None, st="new")]
    led = w.ledger(orders, dt.date(2026, 11, 15), 1.0)
    assert len(led["lots"]) == 2 and led["lots"][1]["sell"] is None and led["lots"][1]["done"]
    assert round(led["cash"], 4) == round(175 - 90 + 0.9975 * 100.8 - 42.5, 4)


def test_price_format():
    assert fmt_price(1899.8) == "1899.8" and fmt_price(75.4) == "75.40" and fmt_price(0.06281) == "0.062810"
