# Sean Realistisch: trend-following ETF bot (Alpaca)

A moderately aggressive, **no-margin** bot. It holds SPY/QQQ, switches to 2× ETFs (SSO/QLD) only in a strong bull market, and holds GLD or cash otherwise.

| | |
|---|---|
| Broker | Alpaca via `alpaca-py`. `PAPER = True` (default) uses the paper endpoint. |
| Capital | $500 start + $300 on the 1st of every month |
| Sizing | Fractional (notional) orders. 2% risk per trade, max 40% of equity per position, at most 100% of cash in total (no borrowing). |
| Positions | At most 3: one per sleeve (SPY/SSO, QQQ/QLD, GLD) |
| Schedule | Every 30 min on weekdays. It only trades 9:45–15:50 New York time, when the market is open. |
| Order label | `sr-` |

## Rules

1. **Regime (Paul Tudor Jones / Ed Seykota).** Daily bars of the index (SPY, QQQ or GLD). Long only when price > 200-day SMA **and** 50-day SMA > 200-day SMA (golden cross).
   - Price above both, with the 20-day EMA rising: buy the **2× ETF** (SSO / QLD).
   - Price between the 200-day and 50-day SMA, or the 20-day EMA falling: buy the **1× ETF** (SPY / QQQ).
   - Otherwise: no new trades, and open positions in that sleeve are sold (rotate to cash).
2. **Entries (Larry Connors / Richard Dennis).**
   - **A, pullback:** RSI(2) < 20 while price is above the 200-day SMA.
   - **B, breakout:** close at a 20-day high, on ≥1.3× the 20-day average volume.
   - Signals come from the previous completed daily candle; the buy happens the next session.
3. **Sizing and pyramiding (Jesse Livermore / Mark Minervini).**
   - Stop at entry − 2×ATR(14).
   - `Position_Value = Equity × 2% / ((2 × ATR) / Entry)`, capped at 40% of equity and at the free cash.
   - At +1.5 ATR: move the stop to breakeven and add 50% of the first unit.
4. **Exits (Stanley Druckenmiller).**
   - Sell 50% when RSI(2) > 85 or profit ≥ 2.5× the initial risk.
   - The rest rides with a trailing stop of 2.5×ATR below the highest price.
5. **Circuit breaker.** Equity −6% from its 7-day high (corrected for deposits): sell the 2× ETFs and pause new entries for 72 hours.

**Fractional shares:** Alpaca only accepts **day** stop orders for fractional shares, so the bot re-places the stops at every session. Between the close and the next open there is no stop: a gap down can fill below the stop level.

## Files

| File | What it does |
|---|---|
| `config.py` | All settings, including `PAPER` and `CAPITAL_MODE` |
| `indicators.py` | SMA, EMA, RSI(2), ATR, regime and entry signals |
| `risk_manager.py` | Sizing, stops, pyramiding, take-profit, trailing stop, deposits, circuit breaker |
| `broker_alpaca.py` | alpaca-py: retries on rate limits and server errors, market hours, fractional orders, day stops |
| `main.py` | One cycle (or `--loop`); its own ledger from its `sr-` orders; state in `state/state.json` |
| `notifier.py` | Telegram / Discord: buys, sells, stop-losses, trailing-stop updates, pyramids, deposits, breaker, errors |
| `backtest.py` | 24 months of daily data with $500 + $300/month, compared with "same deposits into SPY" |

## Capital mode

Your paper account holds $100k of fake money and is shared with the other bots. In paper mode the bot therefore keeps a **virtual ledger**: $500 + $300 per month + its own P&L. With `PAPER = false` on its own live account it uses `get_account().equity` and detects deposits from Alpaca's cash activities.
