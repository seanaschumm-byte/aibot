"""Weekly mean-reversion grid: pure functions, no network.

Ideas taken from known strategies (see README, "Weekly dip bot"):
- Bollinger / mean reversion: the weekly average price (7-day VWAP) is the 'fair' middle;
  -10% under it is the user's buy zone.
- Grid trading: several resting limit buys below the average, each with its own fixed take-profit.
- Value averaging: put MORE money in at deeper levels (20% / 25% / 25% / 30%).
- DCA: fixed weekly budget, unused money carries over.
- John Bollinger: in volatile weeks the deeper levels drop to 2/3/4 standard deviations under
  the average (Bollinger bands), so a crash is bought cheaper. Level 1 stays at -10%.
- Larry Connors: exit near the mean. Buying at -10% and selling at +12% sells at about the
  weekly average, which is how Connors exits mean-reversion trades.
- Contrarian 'buy the crash' (Rothschild / Buffett "be greedy when others are fearful"):
  no crash filter, plus an extra deep level at -30% to buy into real crashes.
"""
from __future__ import annotations

LEVELS = [(-0.10, 0.20), (-0.15, 0.25), (-0.20, 0.25), (-0.30, 0.30)]   # (distance from weekly avg, budget share)
# John Bollinger: in wild weeks the deeper levels move down to avg - k * sigma (Bollinger band).
# Level 1 always stays at the user's -10%.
BAND_SIGMAS = [None, 2.0, 3.0, 4.0]
TAKE_PROFIT = 0.12
CRASH_30D = -0.35   # only used to LABEL a crash in the log; crashed coins are still bought


def weekly_vwap(bars: list[dict], hours: int = 168) -> float:
    w = bars[-hours:]
    vol = sum(b["v"] for b in w)
    if not vol:
        return sum(b["c"] for b in w) / len(w)
    return sum((b.get("vw") or (b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in w) / vol


def weekly_sigma(bars: list[dict], hours: int = 168) -> float:
    """Standard deviation of hourly closes over the week (the Bollinger band width)."""
    c = [b["c"] for b in bars[-hours:]]
    m = sum(c) / len(c)
    return (sum((x - m) ** 2 for x in c) / len(c)) ** 0.5


def level_price(avg: float, sigma: float, i: int) -> float:
    """Fixed % level, or the Bollinger band if that is lower (only for levels 2-4)."""
    dist, _ = LEVELS[i]
    price = avg * (1 + dist)
    k = BAND_SIGMAS[i]
    if k is not None and sigma > 0:
        price = min(price, avg - k * sigma)
    return max(price, avg * 0.05)


def crashed(bars: list[dict], hours: int = 720) -> bool:
    if len(bars) < hours:
        return False
    return bars[-1]["c"] / bars[-hours]["c"] - 1 < CRASH_30D


def plan_orders(avg_by_coin: dict[str, float], cash: float, min_order: float = 1.0,
                sigma_by_coin: dict[str, float] | None = None) -> list[dict]:
    """Split the available cash equally over coins, and per coin over the levels."""
    if not avg_by_coin or cash <= 0:
        return []
    per_coin = cash / len(avg_by_coin)
    out = []
    for coin, avg in avg_by_coin.items():
        sigma = (sigma_by_coin or {}).get(coin, 0.0)
        for i, (_, share) in enumerate(LEVELS):
            usd = per_coin * share
            if usd < min_order:
                continue
            price = level_price(avg, sigma, i)
            out.append({"coin": coin, "level": i + 1, "dist": price / avg - 1, "limit": price,
                        "usd": usd, "qty": usd / price})
    return out


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:.1f}"
    if p >= 1:
        return f"{p:.2f}"
    return f"{p:.6f}"
