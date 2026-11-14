# Sean Lavarge: leveraged trend-breakout futures bot

⚠️ **Leverage can lose money fast.** Run it on the demo exchange (`TESTNET = True`, the default) for at least 30 days before even thinking about real euros.

| | |
|---|---|
| Exchange | Binance USD-M Futures via `ccxt`: the **testnet** while `TESTNET`. Configurable with `EXCHANGE_ID`. Kraken's demo and Bybit's testnet were not reachable from GitHub. |
| Markets | BTC, ETH and SOL perpetual futures (USDT-margined), long and short |
| Capital | €500 start + €150 every Monday 00:00 UTC. New deposits are locked for 7 days (deposit shield). |
| Leverage | Max 10×, isolated margin, set per symbol before each entry |
| Schedule | Every 15 minutes (GitHub Actions), or `python -m sean_lavarge.main --loop` on your own PC |

## Rules

1. **Trend + breakout (Richard Dennis / Turtles).** Long only above the 4h 200-EMA; short only below it. Entry on a close beyond the 20-bar Donchian channel, with volume above 1.5× its 20-bar average.
2. **Risk (Paul Tudor Jones / Ed Seykota).** 1.5% of risk equity per trade: `size = equity × 0.015 / (2 × ATR14)`. Notional is capped at 10× equity and must fit the free isolated margin. If the cap applies, the trade risks less than 1.5%.
3. **Stops and pyramiding (Jesse Livermore / Stanley Druckenmiller).** A hard reduce-only stop is placed on the exchange at entry ± 2 ATR. It never adds to a loser. At +1.5 ATR it adds one unit of 50% size and moves the stop to breakeven.
4. **Exit (Turtle System 1).** Close when the 4h candle closes beyond the opposite 10-bar channel. Your spec only had a stop, so this lets winners run without giving everything back.
5. **Circuit breakers (Mark Minervini).** Equity −4.5% within 24h, or 3 losses in a row within 24h, pauses new trades for 24 hours. Open positions stay protected by their stops.

## Files

| File | What it does |
|---|---|
| `config.py` | All settings, including `TESTNET` |
| `strategy.py` | Indicators and signals (pandas) |
| `risk_manager.py` | Sizing, stops, pyramiding, deposits, circuit breakers |
| `exchange_client.py` | ccxt wrapper: retries on rate limits and network drops, isolated leverage, stop orders |
| `notifier.py` | Discord and/or Telegram alerts for entries, pyramids, stops, exits, breakers, weekly balance and errors |
| `main.py` | One cycle, or `--loop`. State lives in `state/state.json`. |
| `backtest.py` | 12 months of Coinbase BTC/EUR, ETH/EUR and SOL/EUR 4h data: €500 + €150/week, fees, slippage and funding. Reports win rate, profit factor, max drawdown and Sharpe. |
| `check.py` | Connectivity check with the exchange (no keys needed) |

## Setup (demo)

1. Go to https://testnet.binancefuture.com, log in, and copy the **API Key** and **Secret Key** shown under the order panel (*API Key* tab). This is fake money and separate from a real Binance account.
2. In GitHub, go to **Settings → Secrets → Actions** and add `SEAN_EXCHANGE_API_KEY` and `SEAN_EXCHANGE_SECRET`.
3. Optional alerts: add `DISCORD_WEBHOOK_URL`, or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.
4. The **Sean Lavarge** workflow runs every 15 minutes. **Run workflow** with `check`, `run` or `backtest` runs it by hand.

The demo account has a large fake balance. In demo mode the bot therefore keeps a **virtual ledger**: €500 + €150 per week + its own realised P&L. Sizes match what you would trade with real money.

## Before real money

- Run 30+ days on demo and compare the results with the backtest.
- **EU rules:** Binance doesn't serve the Netherlands, and many exchanges don't offer perpetual futures to EU residents. For real money you'd need a licensed EU venue that offers leveraged crypto products, often with a lower maximum leverage. Set `EXCHANGE_ID` to it.
- For live trading, set `TESTNET: "false"` (then `CAPITAL_MODE` becomes `balance`, so equity comes from `fetch_balance()` and deposits are detected automatically), and use live keys **without withdrawal rights**.
