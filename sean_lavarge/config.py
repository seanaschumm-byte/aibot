"""Sean Lavarge: leveraged trend-breakout futures bot. All settings in one place.

Secrets come from environment variables (GitHub Secrets / your shell), never from this file:
  EXCHANGE_API_KEY, EXCHANGE_SECRET           exchange API keys (demo keys while TESTNET)
  DISCORD_WEBHOOK_URL                          optional Discord alerts
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID         optional Telegram alerts
"""
from __future__ import annotations

import datetime as dt
import os

# ---------------------------------------------------------------- mode
TESTNET = os.getenv("TESTNET", "true").lower() != "false"   # True = demo exchange, fake money
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "krakenfutures")      # any ccxt id with perps, e.g. bybit
# Perpetual futures to trade (ccxt unified symbols for the exchange above).
SYMBOLS = os.getenv("SYMBOLS", "BTC/USD:USD,ETH/USD:USD,SOL/USD:USD").split(",")
QUOTE = os.getenv("QUOTE", "USD")                            # currency of PnL / margin

# ---------------------------------------------------------------- capital (in EUR)
START_DATE = dt.date(2026, 11, 14)
START_CAPITAL_EUR = 500.0
WEEKLY_DEPOSIT_EUR = 150.0          # every Monday 00:00 UTC
DEPOSIT_LOCK_DAYS = 7               # new deposits are not risked until the next weekly cycle
EURUSD = float(os.getenv("EURUSD", "1.10"))   # used for the virtual demo ledger
# "virtual": equity = EUR 500 + Monday deposits + realised PnL (demo accounts have a big fake
#            balance, so this keeps the test realistic).
# "balance": equity = exchange fetch_balance() (use for real money; deposits detected automatically).
CAPITAL_MODE = os.getenv("CAPITAL_MODE", "virtual" if TESTNET else "balance")

# ---------------------------------------------------------------- strategy (Turtle / Dennis)
TIMEFRAME = "4h"
EMA_TREND = 200                     # trade only with the 4h 200-EMA trend
DONCHIAN_ENTRY = 20                 # breakout of the 20-bar channel
DONCHIAN_EXIT = 10                  # Turtle System-1 exit: opposite 10-bar channel
VOLUME_MA = 20
VOLUME_SPIKE = 1.5                  # breakout volume > 1.5x its 20-bar average
ATR_PERIOD = 14
ALLOW_SHORTS = True

# ---------------------------------------------------------------- risk (PTJ / Seykota / Livermore)
RISK_PER_TRADE = 0.015              # max 1.5% of equity lost if the stop is hit
STOP_ATR = 2.0                      # hard stop 2x ATR from entry
MAX_LEVERAGE = 10                   # isolated margin, never more than 10x
PYRAMID_TRIGGER_ATR = 1.5           # +1.5 ATR in profit -> stop to breakeven + add a unit
PYRAMID_FRACTION = 0.5              # pyramid unit = 50% of the first unit
MAX_PYRAMIDS = 1
MAX_OPEN_POSITIONS = 3

# ---------------------------------------------------------------- circuit breakers (Minervini)
DAILY_LOSS_LIMIT = 0.045            # -4.5% equity within 24h -> pause
MAX_CONSECUTIVE_LOSSES = 3          # 3 losses in a row within 24h -> pause
PAUSE_HOURS = 24

# ---------------------------------------------------------------- costs (backtest)
TAKER_FEE = 0.0005                  # 0.05% per side (Kraken Futures taker, low tier)
SLIPPAGE = 0.0005                   # 0.05% per fill
FUNDING_PER_8H = 0.0001             # 0.01% of notional per 8h held (typical perp funding)

# ---------------------------------------------------------------- backtest data
BACKTEST_EXCHANGE = "coinbaseexchange"
BACKTEST_SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]
BACKTEST_DAYS = 365

# ---------------------------------------------------------------- runtime
LOOP_MINUTES = 15
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")
API_RETRIES = 5
