# AI bot: VWAP pullback paper trader

An intraday "buy the dip in an uptrend" bot for an **Alpaca paper account**. GitHub Actions runs it every 5 minutes during US market hours.

- **Budget:** a virtual €75 deposited every Monday from 16 Nov 2026. The bot only spends that money, never the full $100k paper balance.
- **Test period:** 16 Nov to 16 Dec 2026. After that it stops opening trades.
- **Intraday only:** every position is closed by 15:50 New York time.
- **Your trades on Alpaca:** every bot order appears in your paper dashboard with a `client_order_id` starting with `aibot-`.

## The strategy

1. **Market filter.** SPY must be above its VWAP.
2. **Stock in play.** Relative volume must be at least 1.5× normal for that time of day.
3. **Leader.** The stock must be up more than SPY today.
4. **Uptrend.** At least 60% of the session's bars closed above a rising VWAP, and the 9 EMA is above the 20 EMA (5-minute bars).
5. **Dip.** Within the last 4 bars, price pulled back to VWAP: no more than 0.3% above it and no more than 1% below it.
6. **Falling-knife filter.** No wide-range red bar on heavy volume inside the pullback.
7. **Confirmation.** A green bar that closes in the upper half of its range, back above VWAP, with a higher close and higher volume than the previous bar.
8. **Entry.** Fractional-share market buy. No entries before 9:45 or after 15:00.
9. **Stop.** Just under the pullback low, kept between 0.3% and 2% below entry. It is placed with the broker as a stop order.
10. **Target.** 2× the risk (2:1). Once the trade is up 1R, the stop moves to breakeven.
11. **Risk.** 1% of the budget at risk per trade, no margin, one position at a time, at most 3 trades a day, and no new trades after a 2% daily loss.

## Rules taken from ten well-known traders

These are summaries of their public teaching (books, courses, interviews), applied as filters on top of the VWAP pullback. They are not copies of their actual trades.

| Trader | Their principle | Bot rule |
|---|---|---|
| Brian Shannon | VWAP is the average cost of today's participants. Favor longs above a rising VWAP. | Trend and VWAP-slope filter |
| Linda Raschke | "Holy Grail": buy the first pullback in a strong trend | EMA trend; only recent pullbacks |
| Ross Cameron | Trade high relative volume, demand at least 2:1, and set a hard daily max loss | rvol ≥ 1.5, 2R target, 2% daily stop |
| Andrew Aziz | Trade "stocks in play" and use VWAP as the key intraday level | Relative-volume filter, VWAP entries |
| Al Brooks | Enter on a strong signal bar that closes near its high | Signal-bar quality check |
| Mark Minervini | Cut losses fast and never let a good gain turn into a loss | Tight stop, breakeven at +1R |
| Paul Tudor Jones | Defense first, and never average down a loser | One entry per trade, no adding |
| Peter Brandt | Risk about 1% or less of capital per trade | 1% position sizing |
| Jesse Livermore | Trade in the direction of the general market | SPY-above-VWAP filter |
| Kristjan Kullamägi | Trade the strongest leading stocks | Must beat SPY today |

## Setup

1. Push this folder to a **private** GitHub repo.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (your paper keys).
3. Go to **Actions → trading-bot → Enable**. The bot runs on the 5-minute schedule from then on.
4. To check results, go to **Actions → trading-bot → Run workflow** with mode `report`. The summary appears on the run page. Every scheduled run also writes its log and a summary there.

Running every 5 minutes on weekdays uses about 1,850 GitHub Actions minutes a month. A private repo on the free plan includes 2,000.

## Local use

```bash
pip install -r requirements.txt
python -m pytest -q                       # strategy tests, offline
DRY_RUN=1 python bot.py run               # decide but don't place orders
python bot.py report                      # performance since the start date
```

## Limitations

- **IEX data only.** The free Alpaca data feed is IEX, a small share of total volume, so VWAP and volume figures are approximate.
- **Coarse timing.** The bot only checks prices every 5 minutes (sometimes later when GitHub is busy). The stop sits with the broker, so the downside is covered between checks, but targets and the breakeven move only happen on a check.
- **Paper results aren't real results.** Paper fills are optimistic: no slippage and no queue position.
- **Real money is different.** With real money at a US broker, an account under $25k may hit the pattern-day-trader limit of 3 day trades per 5 business days. At about €75 a week, fees and spreads would also take a large share of any gains.
