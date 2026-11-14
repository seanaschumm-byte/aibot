"""Market mood check shared by all bots.

Sources (all free):
- Crypto Fear & Greed Index (alternative.me): 0 = extreme fear ... 100 = extreme greed
- BTC trend: price vs 50-day average and 7-day change (Alpaca crypto data)
- Stock market trend: SPY vs its 50-day average (Alpaca stock data, IEX feed)

Every source is optional: if one fails the bots still run, with neutral settings.
"""
from __future__ import annotations

import datetime as dt

import requests


def classify_fng(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < 25:
        return "extreme_fear"
    if value < 45:
        return "fear"
    if value <= 55:
        return "neutral"
    if value <= 75:
        return "greed"
    return "extreme_greed"


def fear_greed() -> tuple[int | None, str]:
    try:
        js = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()
        v = int(js["data"][0]["value"])
        return v, classify_fng(v)
    except Exception:
        return None, "unknown"


def trend_from_daily(closes: list[float]) -> dict:
    """Price vs 50-day average, and 7-day change."""
    if len(closes) < 50:
        return {}
    sma50 = sum(closes[-50:]) / 50
    return {"price": closes[-1], "vs_sma50": closes[-1] / sma50 - 1,
            "chg_7d": closes[-1] / closes[-8] - 1, "uptrend": closes[-1] > sma50}


def _daily_closes(api, data_url: str, path: str, symbol: str, extra: dict) -> list[float]:
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).isoformat()
    js = api("GET", path, base=data_url, params={"symbols": symbol, "timeframe": "1Day",
                                                 "start": start, "limit": 1000, **extra})
    return [b["c"] for b in (js.get("bars") or {}).get(symbol, [])]


def mood(api, data_url: str) -> dict:
    """Snapshot of the whole market. Never raises."""
    fng, label = fear_greed()
    out = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
           "fear_greed": fng, "fear_greed_label": label}
    try:
        out["btc"] = trend_from_daily(_daily_closes(api, data_url, "/v1beta3/crypto/us/bars",
                                                    "BTC/USD", {}))
    except Exception:
        out["btc"] = {}
    try:
        out["spy"] = trend_from_daily(_daily_closes(api, data_url, "/v2/stocks/bars", "SPY",
                                                    {"feed": "iex", "adjustment": "all"}))
    except Exception:
        out["spy"] = {}
    out["btc_crash"] = out["btc"].get("chg_7d", 0) < -0.15
    return out


# ---- how each bot reacts to the mood (small, explainable adjustments) ----

def crypto_size_factor(m: dict) -> float:
    """Daily dip bot: buy smaller when the market is greedy (a pullback is more likely)."""
    return {"extreme_greed": 0.5, "greed": 0.75}.get(m.get("fear_greed_label"), 1.0)


def weekly_plan(m: dict) -> tuple[float, list[float]]:
    """Weekly dip bot: (share of available cash to use, budget weights for -10/-15/-20/-30%).
    Extreme fear -> more money on the deep crash levels (Buffett: greedy when others are fearful).
    Greed -> use less of the budget this week; the rest carries over to next week."""
    label = m.get("fear_greed_label")
    if label == "extreme_fear":
        return 1.0, [0.10, 0.20, 0.30, 0.40]
    if label == "extreme_greed":
        return 0.5, [0.20, 0.25, 0.25, 0.30]
    if label == "greed":
        return 0.75, [0.20, 0.25, 0.25, 0.30]
    return 1.0, [0.20, 0.25, 0.25, 0.30]


def stock_risk_factor(m: dict) -> float:
    """Stock bot: half risk per trade while SPY is below its 50-day average (weak market)."""
    spy = m.get("spy") or {}
    return 0.5 if spy and not spy.get("uptrend", True) else 1.0


def summary_line(m: dict) -> str:
    b, s = m.get("btc") or {}, m.get("spy") or {}
    parts = [f"Fear&Greed {m.get('fear_greed')} ({m.get('fear_greed_label')})"]
    if b:
        parts.append(f"BTC {b['vs_sma50']:+.1%} vs 50d avg, 7d {b['chg_7d']:+.1%}")
    if s:
        parts.append(f"SPY {s['vs_sma50']:+.1%} vs 50d avg")
    if m.get("btc_crash"):
        parts.append("BTC CRASH (>15% down in 7d)")
    return "MARKET: " + "; ".join(parts)
