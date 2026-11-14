"""Offline tests for the market mood check and the trade journal."""
import json
import journal
import market


def test_fear_greed_labels():
    assert [market.classify_fng(v) for v in (10, 30, 50, 70, 90, None)] == \
        ["extreme_fear", "fear", "neutral", "greed", "extreme_greed", "unknown"]


def test_trend():
    up = market.trend_from_daily([100 + i for i in range(60)])
    assert up["uptrend"] and up["chg_7d"] > 0
    assert market.trend_from_daily([1.0] * 10) == {}


def test_reactions():
    assert market.crypto_size_factor({"fear_greed_label": "extreme_greed"}) == 0.5
    assert market.crypto_size_factor({"fear_greed_label": "unknown"}) == 1.0
    use, w = market.weekly_plan({"fear_greed_label": "extreme_fear"})
    assert use == 1.0 and w[-1] > w[0] and abs(sum(w) - 1) < 1e-9
    use, w = market.weekly_plan({"fear_greed_label": "extreme_greed"})
    assert use == 0.5 and abs(sum(w) - 1) < 1e-9
    assert market.stock_risk_factor({"spy": {"uptrend": False}}) == 0.5
    assert market.stock_risk_factor({"spy": {}}) == 1.0          # no data -> normal


def test_mood_never_raises():
    def broken_api(*a, **k):
        raise RuntimeError("no network")
    m = market.mood(broken_api, "http://x")
    assert m["btc"] == {} and m["spy"] == {} and not m["btc_crash"]
    assert market.summary_line(m).startswith("MARKET:")


def test_journal_write_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "DIR", str(tmp_path))
    monkeypatch.delenv("DRY_RUN", raising=False)
    journal.write("t", "entry", cid="a", mood={"x": 1})
    journal.record_closes("t", [{"cid": "a", "pnl_usd": 1.0}])
    journal.record_closes("t", [{"cid": "a", "pnl_usd": 1.0}, {"cid": "b", "pnl_usd": -1.0}])
    recs = journal.read("t")
    assert [r["event"] for r in recs] == ["entry", "close", "close"]
    assert journal.has("t", "close", "cid", "b")
    journal.daily_market("t", {"m": 1}); journal.daily_market("t", {"m": 2})
    assert sum(r["event"] == "market" for r in journal.read("t")) == 1
    assert all(json.loads(l) for l in open(tmp_path / "t.jsonl"))
