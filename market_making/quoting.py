"""Market-making quote engine (pure functions, no network).

Idea (simplified Avellaneda-Stoikov): quote a bid below and an ask above the mid price. The
half-spread must cover the maker fee on BOTH fills plus a profit margin, and widens with
volatility. Inventory skews both quotes down: the more coins the bot holds, the cheaper it offers
them (sells sooner) and the lower it bids (buys later). Spot only: inventory is never negative.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class MMParams:
    maker_fee: float = 0.0015      # Alpaca crypto maker fee per fill (tier 1)
    min_edge: float = 0.0010       # profit wanted per round trip AFTER fees (0.10%)
    vol_mult: float = 1.5          # half-spread >= 1.5 x one-minute volatility * sqrt(horizon)
    horizon_min: float = 15.0      # quotes live ~15 minutes between re-quotes
    skew: float = 0.004            # full inventory shifts both quotes down 0.4%
    order_usd: float = 25.0        # size per quote (Alpaca minimum is $10)
    max_inventory_usd: float = 250.0
    requote_move: float = 0.0010   # replace a quote if its ideal price moved > 0.10%


def half_spread(sigma_1m: float, p: MMParams = MMParams()) -> float:
    """Fraction of mid. Covers 2 maker fees + min edge, or the volatility band if that is wider."""
    fee_floor = p.maker_fee + p.min_edge / 2          # per side: half of (2 fees + edge)
    vol_band = p.vol_mult * sigma_1m * math.sqrt(p.horizon_min)
    return max(fee_floor, vol_band)


def quotes(mid: float, best_bid: float, best_ask: float, sigma_1m: float, inventory_usd: float,
           cash_usd: float, p: MMParams = MMParams()) -> dict:
    """Bid/ask prices and whether to quote each side."""
    q = max(0.0, min(1.0, inventory_usd / p.max_inventory_usd))   # 0 = flat, 1 = full
    h = half_spread(sigma_1m, p)
    reservation = mid * (1 - p.skew * q)                          # inventory skew
    bid = reservation * (1 - h)
    ask = reservation * (1 + h)
    # never cross the book (a crossing limit order would fill at once as a taker)
    bid = min(bid, best_bid)
    ask = max(ask, best_ask)
    return {"bid": bid, "ask": ask, "half_spread": h, "inv_ratio": q,
            "quote_bid": inventory_usd + p.order_usd <= p.max_inventory_usd and cash_usd >= p.order_usd,
            "quote_ask": inventory_usd >= 10.0}


def sigma_from_closes(closes: list[float]) -> float:
    """Std-dev of 1-minute log returns."""
    r = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(r) < 2:
        return 0.0
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))


def needs_requote(current: float | None, ideal: float, p: MMParams = MMParams()) -> bool:
    return current is None or abs(current / ideal - 1) > p.requote_move
