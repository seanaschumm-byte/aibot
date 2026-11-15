"""AI trading bot Shakur: the 6 distinct strategies from the top 10 of the last 6 months
(15 Nov 2026 test of 21 strategies), each with an equal share of the money.

Top 10 by profit over 6 months (EUR 500 + EUR 100/week):
  1 Bollinger Band bounce, 15 stocks/ETFs   +145  -> sleeve BOLL
  2 Turtle breakout, bitcoin (daily)        +116  -> sleeve TURTLE
  3 Bollinger Band mean reversion, SPY       +76  (same rule as 1, inside BOLL)
  4 Connors RSI(2) pullbacks, SPY            +74  -> sleeve CONN_SPY
  5 Connors RSI(2), crypto                   +27  -> sleeve CONN_CRYPTO
  6 8% dip, sell +8%, 15 stocks/ETFs         +25  -> sleeve DIP8
  7 Buy SPY every week                        +9  (held SPY all period = same as 8)
  8 Meb Faber 200-day trend, SPY              +9  -> sleeve FABER
  9 Golden cross 50/200, SPY                  +9  (held SPY all period = same as 8)
 10 Connors RSI(2) sell at RSI>70, crypto     +8  (same entry as 5)
"""
from __future__ import annotations

import datetime as dt

STOCKS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA",
          "JPM", "XLE", "GLD"]
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD"]

# tag: (strategy name in justin.strategies.ALL_STRATEGIES, universe, max positions, add deposits to open position)
SLEEVES = {
    "BOLL": ("Bollinger Band bounce", STOCKS, 5, False),
    "TURTLE": ("Turtle breakout", ["BTC/USD"], 1, True),
    "CONN_SPY": ("Connors RSI(2)", ["SPY"], 1, False),
    "CONN_CRYPTO": ("Connors RSI(2)", CRYPTO, 3, False),
    "DIP8": ("8% dip, sell +8%", STOCKS, 5, False),
    "FABER": ("Faber 200-day trend", ["SPY"], 1, True),
}
SHARE = 1 / len(SLEEVES)
START = dt.date(2026, 11, 16)
START_EUR, WEEKLY_EUR = 500.0, 100.0
PREFIX = "sk"
CRYPTO_FEE = 0.0025
