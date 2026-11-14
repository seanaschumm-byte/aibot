"""Weekly mean-reversion grid: pure functions, no network.

Ideas taken from known strategies (see README, "Weekly dip bot"):
- Bollinger / mean reversion: the weekly average price (7-day VWAP) is the 'fair' middle;
  -10% under it is the user's buy zone.
- Grid trading: several resting limit buys below the average, each with its own fixed take-profit.
- Value averaging: put MORE money in at deeper levels (20% / 25% / 25% / 30%).
- DCA: fixed weekly budget, unused money carries over.
- Contrarian 'buy the crash' (Rothschild / Buffett "be greedy when others are fearful"):
  no crash filter, plus an extra deep level at -30% to buy into real crashes.
"""
from __future__ import annotations

LEVELS = [(-0.10, 0.20), (-0.15, 0.25), (-0.20, 0.25), (-0.30, 0.30)]   # (distance from weekly avg, budget share)
TAKE_PROFIT = 0.12
CRASH_30D = -0.35   # only used to LABEL a crash in the log; crashed coins are still bought


def weekly_vwap(bars: list[dict], hours: int = 168) -> float:
    w = bars[-hours:]
    vol = sum(b["v"] for b in w)
    if not vol:
        return sum(b["c"] for b in w) / len(w)
    return sum((b.get("vw") or (b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in w) / vol


def crashed(bars: list[dict], hours: int = 720) -> bool:
    if len(bars) < hours:
        return False
    return bars[-1]["c"] / bars[-hours]["c"] - 1 < CRASH_30D


def plan_orders(avg_by_coin: dict[str, float], cash: float,
                min_order: float = 1.0) -> list[dict]:
    """Split the available cash equally over coins, and per coin over the levels."""
    if not avg_by_coin or cash <= 0:
        return []
    per_coin = cash / len(avg_by_coin)
    out = []
    for coin, avg in avg_by_coin.items():
        for i, (dist, share) in enumerate(LEVELS, 1):
            usd = per_coin * share
            if usd < min_order:
                continue
            price = avg * (1 + dist)
            out.append({"coin": coin, "level": i, "dist": dist, "limit": price,
                        "usd": usd, "qty": usd / price})
    return out


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:.1f}"
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.6f}"
