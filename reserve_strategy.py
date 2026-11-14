"""Smart Reserve DCA: opportunistic dip buying from one shared euro pot. Pure functions.

Buy rules (all 4 must hold):
 1. price at least 6% (BTC/ETH) or 10% (alts) under the 7-day high
 2. RSI(14) on 4-hour bars under 30 (BTC/ETH) or 27 (alts)
 3. not in free fall: last 1-hour candle green, or price above the 9-period average on 15-min bars
 4. spread & cooldown: coin under its max % of the pot, no buy of this coin in the last 48 h,
    and if already held: price at least 5% under the previous buy price
Priority: the qualifying coin with the lowest RSI (most oversold).

Sell rules: once price is +8% (BTC/ETH) / +12% (alts) above the coin's average cost, a trailing
take-profit of 1.5% / 2.5% follows the peak; when hit, 100% of the coin goes back to the pot.
"""
from __future__ import annotations

from dataclasses import dataclass

MAJORS = {"BTC/USD", "ETH/USD"}
CAPS = {"BTC/USD": 0.35, "ETH/USD": 0.25, "SOL/USD": 0.10, "XRP/USD": 0.10,
        "LINK/USD": 0.10, "AVAX/USD": 0.10, "ADA/USD": 0.10}


@dataclass
class Rules:
    dip_major: float = 0.06
    dip_alt: float = 0.10
    rsi_major: float = 30.0
    rsi_alt: float = 27.0
    cooldown_h: float = 48.0
    rebuy_gap: float = 0.05
    tp_major: float = 0.08
    tp_alt: float = 0.12
    trail_major: float = 0.015
    trail_alt: float = 0.025


def rsi(closes: list[float], n: int = 14) -> float:
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return 50.0
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def check_buy(coin: str, price: float, h1: list[dict], h4: list[dict], m15: list[dict],
              coin_value: float, pot_value: float, order_usd: float,
              hours_since_buy: float | None, last_buy_price: float | None,
              r: Rules = Rules()) -> tuple[bool, str, float]:
    """Return (ok, reason, rsi)."""
    major = coin in MAJORS
    if len(h1) < 168 or len(h4) < 20 or len(m15) < 9:
        return False, "not enough data", 50.0
    high7 = max(b["h"] for b in h1[-168:])
    drop = price / high7 - 1
    need = r.dip_major if major else r.dip_alt
    rs = rsi([b["c"] for b in h4])
    if drop > -need:
        return False, f"only {drop:.1%} under 7d high (need -{need:.0%})", rs
    limit = r.rsi_major if major else r.rsi_alt
    if rs >= limit:
        return False, f"RSI {rs:.0f} >= {limit:.0f}", rs
    last_h = h1[-1]
    sma9 = sum(b["c"] for b in m15[-9:]) / 9
    if not (last_h["c"] > last_h["o"] or price > sma9):
        return False, "still falling (red 1h candle, under 15m SMA9)", rs
    if pot_value > 0 and (coin_value + order_usd) / pot_value > CAPS[coin]:
        return False, f"cap reached ({coin_value / pot_value:.0%} of pot, max {CAPS[coin]:.0%})", rs
    if hours_since_buy is not None and hours_since_buy < r.cooldown_h:
        return False, f"cooldown ({hours_since_buy:.0f}h since last buy, need {r.cooldown_h:.0f}h)", rs
    if last_buy_price and price > last_buy_price * (1 - r.rebuy_gap):
        return False, f"not 5% under previous buy ({last_buy_price:.4g})", rs
    return True, f"BUY: {drop:.1%} under 7d high, RSI {rs:.0f}", rs


def check_sell(coin: str, avg_cost: float, price: float, bars_since_buy: list[dict],
               r: Rules = Rules()) -> tuple[bool, str]:
    """Trailing take-profit. bars_since_buy: price bars after the latest buy (any timeframe)."""
    major = coin in MAJORS
    target = avg_cost * (1 + (r.tp_major if major else r.tp_alt))
    trail = r.trail_major if major else r.trail_alt
    peak = max([b["h"] for b in bars_since_buy] + [price])
    if peak < target:
        return False, f"target {target:.4g} not reached (peak {peak:.4g})"
    stop = peak * (1 - trail)
    if price <= stop:
        return True, f"trailing TP: peak {peak:.4g}, now {price:.4g} <= {stop:.4g} ({price / avg_cost - 1:+.1%})"
    return False, f"TP active: riding the rise, sells under {stop:.4g} ({price / avg_cost - 1:+.1%})"
