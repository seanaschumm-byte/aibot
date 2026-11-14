"""Position sizing, stops, pyramiding and circuit breakers. Pure functions + small state helpers."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from . import config as C


@dataclass
class Size:
    amount: float          # base units (BTC, ETH, SOL)
    notional: float        # amount * price
    leverage: float        # notional / risk equity
    risk: float            # money lost if the stop is hit
    capped: bool           # True if the 10x leverage cap reduced the size


def position_size(risk_equity: float, atr: float, price: float,
                  free_margin: float | None = None) -> Size:
    """Position_Size = (Equity * 1.5%) / (2 * ATR), then capped so notional <= 10x equity
    and the isolated margin (notional / 10) fits in the free margin."""
    if risk_equity <= 0 or atr <= 0 or price <= 0:
        return Size(0, 0, 0, 0, False)
    amount = risk_equity * C.RISK_PER_TRADE / (C.STOP_ATR * atr)
    max_notional = risk_equity * C.MAX_LEVERAGE
    if free_margin is not None:
        max_notional = min(max_notional, free_margin * C.MAX_LEVERAGE * 0.95)
    capped = amount * price > max_notional
    if capped:
        amount = max_notional / price
    notional = amount * price
    return Size(amount, notional, notional / risk_equity, amount * C.STOP_ATR * atr, capped)


def stop_price(entry: float, atr: float, side: str) -> float:
    return entry - C.STOP_ATR * atr if side == "long" else entry + C.STOP_ATR * atr


def pyramid_due(entry: float, atr: float, price: float, side: str, pyramids: int) -> bool:
    """Livermore / Druckenmiller: add only to a WINNER, after +1.5 ATR."""
    if pyramids >= C.MAX_PYRAMIDS:
        return False
    move = (price - entry) if side == "long" else (entry - price)
    return move >= C.PYRAMID_TRIGGER_ATR * atr


def leverage_for(notional: float, risk_equity: float) -> int:
    """Exchange leverage setting: just enough for this position, max 10x."""
    if risk_equity <= 0:
        return 1
    return max(1, min(C.MAX_LEVERAGE, int(-(-notional // risk_equity))))   # ceil


# ---------------------------------------------------------------- capital
def scheduled_deposits(now: dt.datetime) -> list[tuple[dt.datetime, float]]:
    """EUR 500 at start, then EUR 150 every Monday 00:00 UTC (amounts in EUR)."""
    start = dt.datetime.combine(C.START_DATE, dt.time(), dt.timezone.utc)
    out = [(start, C.START_CAPITAL_EUR)] if now >= start else []
    monday = start + dt.timedelta(days=(7 - start.weekday()) % 7 or 7)
    while monday <= now:
        out.append((monday, C.WEEKLY_DEPOSIT_EUR))
        monday += dt.timedelta(days=7)
    return out


def locked_amount(deposits: list[tuple[dt.datetime, float]], now: dt.datetime,
                  initial: bool = True) -> float:
    """Deposit shield: weekly deposits younger than 7 days are not risked (the start capital is)."""
    lock = dt.timedelta(days=C.DEPOSIT_LOCK_DAYS)
    return sum(a for i, (t, a) in enumerate(deposits) if now - t < lock and (i > 0 or not initial))


# ---------------------------------------------------------------- circuit breakers
def breaker(closed_trades: list[dict], equity_24h_ago: float | None, equity_now: float,
            now: dt.datetime) -> tuple[bool, str]:
    """Minervini: pause 24h after -4.5% equity in 24h, or 3 consecutive losses within 24h.
    closed_trades: dicts with 'closed' (ISO time) and 'pnl', oldest first."""
    if equity_24h_ago and equity_now < equity_24h_ago * (1 - C.DAILY_LOSS_LIMIT):
        return True, f"equity fell {equity_now / equity_24h_ago - 1:.1%} in 24h (limit -{C.DAILY_LOSS_LIMIT:.1%})"
    day_ago = now - dt.timedelta(hours=24)
    recent = [t for t in closed_trades
              if dt.datetime.fromisoformat(t["closed"]) >= day_ago]
    last = recent[-C.MAX_CONSECUTIVE_LOSSES:]
    if len(last) == C.MAX_CONSECUTIVE_LOSSES and all(t["pnl"] < 0 for t in last):
        return True, f"{C.MAX_CONSECUTIVE_LOSSES} losses in a row within 24h"
    return False, ""
