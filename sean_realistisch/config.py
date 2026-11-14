"""Sean Realistisch: trend-following ETF bot for Alpaca (SPY/QQQ, 2x SSO/QLD in strong
bull markets, GLD or cash otherwise). All settings in one place.

Secrets come from environment variables: ALPACA_API_KEY, ALPACA_SECRET_KEY,
optional DISCORD_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import datetime as dt
import os

# ---------------------------------------------------------------- mode
PAPER = os.getenv("PAPER", "true").lower() != "false"        # True = Alpaca paper endpoint
# "virtual": equity = $500 + $300 per month + this bot's own P&L (the paper account is shared with
#            other bots and holds $100k of fake money, so this keeps sizes realistic).
# "account": equity = get_account().equity (use with a dedicated live account).
CAPITAL_MODE = os.getenv("CAPITAL_MODE", "virtual" if PAPER else "account")

# ---------------------------------------------------------------- capital (USD)
START_DATE = dt.date(2026, 11, 14)
START_CAPITAL = 500.0
MONTHLY_DEPOSIT = 300.0              # on the 1st of every month

# ---------------------------------------------------------------- universe
# Each sleeve: the index that decides the regime/signals, the 1x ETF and the 2x ETF.
SLEEVES = {
    "SPY": {"index": "SPY", "x1": "SPY", "x2": "SSO"},
    "QQQ": {"index": "QQQ", "x1": "QQQ", "x2": "QLD"},
    "GLD": {"index": "GLD", "x1": "GLD", "x2": None},     # defensive: gold, never leveraged
}
LEVERAGED = {"SSO", "QLD"}

# ---------------------------------------------------------------- regime (PTJ / Seykota)
SMA_FAST, SMA_SLOW, EMA_SHORT = 50, 200, 20

# ---------------------------------------------------------------- entries (Connors / Dennis)
RSI_PERIOD = 2
RSI_ENTRY = 20.0                     # Entry A: RSI(2) < 20 above the 200 SMA
BREAKOUT_DAYS = 20                   # Entry B: close at a 20-day high ...
BREAKOUT_VOLUME = 1.3                # ... on >= 1.3x the 20-day average volume
MAX_POSITIONS = 3

# ---------------------------------------------------------------- risk (Livermore / Minervini)
RISK_PER_TRADE = 0.02                # 2% of equity lost if the stop is hit
ATR_PERIOD = 14
STOP_ATR = 2.0
MAX_POSITION_PCT = 0.40              # one position <= 40% of equity
PYRAMID_ATR = 1.5                    # +1.5 ATR: stop to breakeven, add 50% of the first unit
PYRAMID_FRACTION = 0.5

# ---------------------------------------------------------------- exits (Druckenmiller)
TP_RSI = 85.0                        # sell 50% when RSI(2) > 85 ...
TP_R_MULTIPLE = 2.5                  # ... or profit reaches 2.5x the initial risk
TP_FRACTION = 0.5
TRAIL_ATR = 2.5                      # trailing stop for the rest
EXIT_WHEN_REGIME_OFF = True          # sell when the index falls out of the golden-cross regime

# ---------------------------------------------------------------- circuit breaker
BREAKER_DRAWDOWN = 0.06              # -6% within any 7 days ...
BREAKER_DAYS = 7
PAUSE_HOURS = 72                     # ... close 2x ETFs and pause entries for 72h

# ---------------------------------------------------------------- runtime
ENTRY_WINDOW = (dt.time(9, 45), dt.time(15, 50))   # New York time, regular session only
PREFIX = "sr"                        # client_order_id prefix of this bot's orders
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")
API_RETRIES = 5
BACKTEST_MONTHS = 24
