"""Sizing, stops, pyramiding, take-profit, deposits and the circuit breaker (pure functions)."""
from __future__ import annotations

import datetime as dt

from . import config as C


def position_value(equity: float, atr: float, entry: float, cash: float) -> float:
    """Position_Value = (Equity * 2%) / ((2 * ATR) / Entry), capped at 40% of equity and the free
    cash (no margin)."""
    if equity <= 0 or atr <= 0 or entry <= 0:
        return 0.0
    value = equity * C.RISK_PER_TRADE / ((C.STOP_ATR * atr) / entry)
    return max(0.0, min(value, C.MAX_POSITION_PCT * equity, cash))


def initial_stop(entry: float, atr: float) -> float:
    return entry - C.STOP_ATR * atr


def pyramid_due(entry: float, atr: float, price: float, done: bool) -> bool:
    return not done and price >= entry + C.PYRAMID_ATR * atr


def take_profit_due(entry: float, atr: float, price: float, rsi2: float, done: bool) -> str | None:
    """Sell 50% once: RSI(2) > 85, or profit >= 2.5x the initial risk (2 ATR)."""
    if done:
        return None
    if rsi2 > C.TP_RSI:
        return f"RSI(2) {rsi2:.0f} > {C.TP_RSI:.0f}"
    if price >= entry + C.TP_R_MULTIPLE * C.STOP_ATR * atr:
        return f"profit >= {C.TP_R_MULTIPLE}R"
    return None


def trailing_stop(current: float, highest: float, atr: float) -> float:
    """Stop only moves up: highest close - 2.5 ATR."""
    return max(current, highest - C.TRAIL_ATR * atr)


def scheduled_deposits(today: dt.date) -> list[tuple[dt.date, float]]:
    """$500 on the start date, then $300 on the 1st of every following month."""
    if today < C.START_DATE:
        return []
    out = [(C.START_DATE, C.START_CAPITAL)]
    y, m = C.START_DATE.year, C.START_DATE.month
    while True:
        m += 1
        if m == 13:
            y, m = y + 1, 1
        d = dt.date(y, m, 1)
        if d > today:
            return out
        out.append((d, C.MONTHLY_DEPOSIT))


def breaker(equity_history: list[tuple[str, float]], equity_now: float) -> tuple[bool, str]:
    """-6% from the highest equity of the last 7 days (net of deposits handled by the caller)."""
    if not equity_history:
        return False, ""
    peak = max(e for _, e in equity_history)
    if peak > 0 and equity_now < peak * (1 - C.BREAKER_DRAWDOWN):
        return True, f"equity {equity_now / peak - 1:.1%} vs 7-day high (limit -{C.BREAKER_DRAWDOWN:.0%})"
    return False, ""
