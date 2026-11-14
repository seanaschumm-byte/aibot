import manual_guard as g


def test_plan_triggers_only_at_minus_40():
    orders = [{"client_order_id": "manual25-B-SOL-1", "symbol": "SOL/USD", "filled_avg_price": "100", "status": "filled"},
              {"client_order_id": "manual25-S-SOL-2", "symbol": "SOL/USD", "status": "new", "qty": "1"},
              {"client_order_id": "manual25-B-ETH-1", "symbol": "ETH/USD", "filled_avg_price": "2000", "status": "filled"},
              {"client_order_id": "manual25-S-ETH-2", "symbol": "ETH/USD", "status": "new", "qty": "1"},
              {"client_order_id": "dip25-S-SOL-9", "symbol": "SOL/USD", "status": "new", "qty": "1"}]
    p = {x["symbol"]: x for x in g.plan(orders, {"SOLUSD": 59.9, "ETHUSD": 1300})}
    assert p["SOL/USD"]["hit"] and round(p["SOL/USD"]["stop"], 2) == 60.0
    assert not p["ETH/USD"]["hit"]                       # -35%: not yet
    assert len(p) == 2                                   # other bots' orders untouched
