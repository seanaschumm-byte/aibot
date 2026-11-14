# AI trading bots (Alpaca paper)

**📊 Check each bot separately: [STATUS.md](STATUS.md)**, refreshed every hour.

| # | Bot | Market | Order label |
|---|---|---|---|
| 1 | Stocks VWAP dip | US stocks, intraday | `aibot-` |
| 2 | BTC risk | BTC breakouts | `btcbot-` |
| 3 | Crypto daily dip | €25/day, sell +20% | `dip25-` |
| 4 | Weekly dip | €175/week grid, sell +12% | `dip10-` |
| $ 5 | Smart Reserve | €500 + €100/week pot, €25 per dip | `rsv-` |
| 6 | **Sean Lavarge** (`sean_lavarge/`) | BTC/ETH/SOL futures, up to 10× isolated, long + short. Binance testnet, not Alpaca. | Binance testnet |
| 7 | **Sean Realistisch** (`sean_realistisch/`) | SPY/QQQ, 2× SSO/QLD in a bull market, GLD. $500 + $300/month, no margin. | `sr-` |

# Bot 1 - Stocks VWAP dip (`bot.py`)

An intraday "buy the dip in an uptrend" bot for an **Alpaca paper account**. GitHub Actions runs it every 10 minutes during US market hours.

- **Budget:** a virtual €75 deposited every Monday from 16 Nov 2026. The bot only spends that money, never the full $100k paper balance.
- **Test period:** 16 Nov to 16 Dec 2026. After that it stops opening trades.
- **Intraday only:** every position is closed from 15:40 New York time.
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

The stock bot (every 10 minutes on weekdays, about 920 minutes a month) and the BTC bot (hourly, about 720) together use about 1,650 GitHub Actions minutes a month. A private repo on the free plan includes 2,000.

## Local use

```bash
pip install -r requirements.txt
python -m pytest -q                       # strategy tests, offline
DRY_RUN=1 python bot.py run               # decide but don't place orders
python bot.py report                      # performance since the start date
```

## Limitations

- **IEX data only.** The free Alpaca data feed is IEX, a small share of total volume, so VWAP and volume figures are approximate.
- **Coarse timing.** The bot only checks prices every 10 minutes (sometimes later when GitHub is busy). The stop sits with the broker, so the downside is covered between checks, but targets and the breakeven move only happen on a check.
- **Paper results aren't real results.** Paper fills are optimistic: no slippage and no queue position.
- **Real money is different.** With real money at a US broker, an account under $25k may hit the pattern-day-trader limit of 3 day trades per 5 business days. At about €75 a week, fees and spreads would also take a large share of any gains.

---

# Bot 2 - BTC risk (`crypto_bot.py`)

An aggressive Bitcoin trend bot on the same Alpaca paper account. It runs **every hour, 24/7**, starting 14 Nov 2026.

- **5 trades of €100 each**, one at a time, with all €100 in at once.
- It **shuts down for good** after 5 trades, or once total profit reaches **+500%** (€2,500 on the €500 stake).
- Any single trade that reaches +500% is sold immediately.
- There's **no leverage**: Alpaca crypto is spot only, so a trade gains or loses exactly what BTC does.
- Its orders carry a `client_order_id` starting with `btcbot-`.

## Strategy (1-hour bars)

1. **Trend:** price above the 200 EMA, and the 20 EMA above the 50 EMA.
2. **Breakout:** the hourly close is above the highest high of the previous 20 hours.
3. **Volume:** the breakout bar's volume is at least 1.5× the 20-hour average, and its range is at least 1× ATR.
4. **Entry:** a €100 market buy.
5. **Initial stop:** 2× ATR below entry, placed with the broker as a stop-limit order.
6. **Trailing stop:** the highest close since entry minus 3× ATR. It only moves up. There's no profit target, so a winner can ride a big trend.

## Rules from ten famous high-risk / big-winner traders

These are summaries of their public teaching, not copies of their trades.

| Trader | Known for | Bot rule |
|---|---|---|
| Richard Dennis | Turtle Traders: turned a small stake into a reported ~$200M on breakouts | 20-bar breakout entry, 2-ATR stop |
| Jesse Livermore | Huge bets on big trends, letting winners run | No profit cap: trailing stop only |
| Stanley Druckenmiller | "Go for the jugular" when conviction is high | Full €100 in at once, no scaling |
| George Soros | Big concentrated bets, e.g. against the pound in 1992 | One concentrated position, no diversification |
| Paul Tudor Jones | Uses the 200-day MA as his "metric for everything" | 200 EMA trend filter |
| Ed Seykota | Trend follower: "cut losses, ride winners" | EMA 20/50 trend + wide trailing stop |
| Nicolas Darvas | Box breakouts with trailing stops | Breakout from the recent range, stop that only moves up |
| Larry Williams | Turned $10k into over $1.1M in a year (1987 contest) with volatility breakouts | Range-expansion filter on the breakout bar |
| Dan Zanger | Record gains from high-volume breakouts | Volume ≥ 1.5× average on the breakout |
| Kristjan Kullamägi | Grew a small account into tens of millions riding momentum | Let momentum winners run on a trailing stop |

## Honest expectations

Trend-breakout systems typically **lose on most trades** (often 60–70%) and rely on a few big winners. Without leverage, +500% total would need BTC to roughly multiply 6× during the bot's trades, so it's very unlikely. In practice expect a modest gain or loss on the €500.

---

# Bot 3 - Crypto daily dip (`dip_bot.py`)

The same "buy the dip in an uptrend" idea as the stock bot, but for crypto, 24/7.

- **At most one €25 buy per day** (UTC day), from 14 Nov to 14 Dec 2026. If no coin shows a good setup that day, it buys nothing.
- **Every buy gets its own sell order at +20%.** It stays open (GTC) until it fills, with no stop-loss.
- **Coins:** ETH, SOL, DOGE, XRP, AVAX and LINK. BTC is left out because the BTC bot trades it.
- **Schedule:** checks every hour at :37. Its orders carry a `client_order_id` starting with `dip25-`.

## Rules (1-hour bars)

1. **Uptrend:** price above the 200 EMA, and the 20 EMA above the 50 EMA.
2. **Dip:** in the last 6 hours, price came back to the 24-hour VWAP: no more than 0.5% above it and no more than 3% below it.
3. **No falling knife:** no red bar in the dip with more than 2.5× normal volume.
4. **Confirmation:** a strong green bar that closes in the upper half of its range, back above VWAP, with a higher close and more volume than the previous bar. Entry must be no more than 2% above VWAP (no chasing).
5. **Choice:** if several coins qualify, the one with the strongest 24-hour gain is bought.

---

# Bot 4 - Weekly dip (`weekly_dip_bot.py`)

- **Budget:** €175 per week, from ISO week 46 (14 Nov 2026) through week 50 (13 Dec), so 5 deposits = €875. Money that isn't used rolls over to the next week, and profits are reinvested.
- **Weekly reference price:** every Monday the bot computes each coin's average price (VWAP) over the last 7 days and logs it.
- **Buys:** resting limit orders at **−10%, −15%, −20% and −30%** (crash level) of that average, with 20% / 25% / 25% / 30% of the coin's budget. They wait 24/7, so a sudden crash is caught even between checks.
- **Sells:** each filled buy immediately gets its own limit sell at **+12%**. There's no stop-loss.
- **Weekly reset:** unfilled buys are cancelled each Monday and placed again around the new weekly average.
- **Coins:** ETH, SOL, XRP, DOGE, AVAX and LINK. It **also buys during crashes**: there's no crash filter, and a coin that fell more than 35% in 30 days is only marked as CRASH in the log.
- **Schedule:** checks every hour at :22. Its orders carry a `client_order_id` starting with `dip10-`.

## Compared with known strategies

| Strategy | Idea | Adopted here |
|---|---|---|
| Your rule | Buy −10% under the average, sell +12% | ✅ The core of the bot |
| **Bollinger / mean reversion** | Prices swing around an average and snap back after extremes | ✅ Weekly VWAP as the "middle band"; −10% as the buy zone |
| **Grid trading** (popular on crypto exchanges) | A ladder of limit buys below the price, each with a fixed take-profit | ✅ 4 levels, each lot with its own +12% sell |
| **Value averaging** (Michael Edleson) | Put in more money when prices are lower | ✅ Deeper levels get more money (20/25/25/30%) |
| **DCA** (dollar-cost averaging) | A fixed amount every period | ✅ €175 every week; unused money carries over |
| **"Buy when there's blood in the streets"** (attributed to Baron Rothschild) / Buffett's "be greedy when others are fearful" | Buy when others panic | ✅ Buys on sharp drops and in crashes, with an extra −30% level |
| **Larry Connors' mean reversion** | Buy dips only in healthy markets, not in a collapse | ❌ Deliberately not used: at your request the bot also buys during crashes |
| **John Bollinger** (Bollinger Bands) — *best fit, adopted* | Measure "cheap" in standard deviations, not a fixed % | ✅ Levels 2–4 move down to 2σ / 3σ / 4σ under the weekly average when a coin is wild, so crashes are bought cheaper and the money isn't spent too early. Level 1 always stays at your −10%. |
| **Larry Connors' exit** — *best fit, already in your rules* | Exit mean-reversion trades back at the average ("exit at the mean") | ✅ Buying at −10% and selling at +12% means selling at about the weekly average |
| **Stop-losses** (most trend traders) | Always cap the loss | ❌ Not used. Mean reversion with a stop often sells right at the bottom. The risk is that a coin keeps falling and the money stays stuck. |

**Typical behaviour:** many small wins (+12%) and occasionally a position that stays "underwater" for a long time. It works best when the market moves sideways, and worst in a long downtrend.

---

# Learning from mistakes: market check and trade journal

## Market check (`market.py`)

Before trading, every bot looks at the whole market. Each data source is optional: if one fails, the bot carries on with normal settings.

| Signal | Source |
|---|---|
| Crypto Fear & Greed Index (0 = panic, 100 = greed) | alternative.me (free) |
| BTC trend: price vs its 50-day average, and 7-day change | Alpaca |
| Stock market: SPY vs its 50-day average | Alpaca |

How each bot reacts:

| Bot | Reaction |
|---|---|
| Stock bot | Risks 0.5% per trade instead of 1% while SPY is below its 50-day average |
| Crypto dip bot (€25/day) | Buys 50% of the usual amount in extreme greed, 75% in greed |
| Weekly dip bot | **Extreme fear:** puts more money on the deep levels (10/20/30/40%), following Buffett's "be greedy when others are fearful". **Greed:** uses 75% of the budget, and 50% in extreme greed; the rest carries over to next week. |
| BTC bot | Only logs the market mood. Breakouts need momentum, so greed is not a reason to skip. |

## Trade journal (`journal/*.jsonl`)

After each run, the workflow commits the bot's journal file to the repo: `stocks`, `btc`, `dip25` or `weekly_dip`. Each line is one event:

- `entry`: a buy, with the reasons and market conditions at that moment.
- `close`: a finished trade, with profit in $ (after the sell fee) and in %.
- `week`: the weekly dip bot's average price, volatility and buy levels per coin.
- `market`: one market-mood snapshot per day.

## Learning loop

Every Saturday the scheduled Claude review reads the journals and Alpaca's order history. It looks for patterns, such as "dips bought in extreme fear won more often" or "stock trades after 14:00 lost". It then proposes small rule changes, and **nothing changes unless you approve it**.

Automatic tuning only makes sense after about 30 trades per bot. Before that, patterns are mostly luck.

---

# $ Bot 5 - Smart Reserve (`reserve_bot.py`)

Opportunistic dip buying from one shared euro pot ("Smart Reserve DCA").

- **Pot:** €500 at the start (14 Nov 2026), plus €100 every Monday. It never buys on a fixed schedule; the money waits for dips. Alpaca has no EUR pairs, so it trades against USD at the live EUR/USD rate.
- **Coins:** BTC, ETH, SOL, XRP, LINK, AVAX and ADA. It scans all 7 **every 15 minutes**.
- **Order size:** €25 per buy signal, at most one buy per scan, spot only (no leverage).
- **Its own coins only:** it keeps its own ledger (`rsv-` orders), so it never sells coins that belong to the other bots.

## Buy rules (all 4 must hold)

| # | Rule | BTC / ETH | Altcoins |
|---|---|---|---|
| 1 | Price below the 7-day high | at least −6% | at least −10% |
| 2 | RSI(14) on 4-hour bars | under 30 | under 27 |
| 3 | Not in free fall | last 1-hour candle green, **or** price above the 9-period average on 15-minute bars | same |
| 4 | Spread and cooldown | under the coin's cap (BTC 35%, ETH 25%, others 10% of the pot); no buy of that coin in the last 48 hours; if already held, at least 5% under the previous buy price | same |

If several coins qualify, it buys the one with the **lowest RSI** (most oversold).

## Sell rules

- The take-profit becomes active at **+8%** (BTC/ETH) or **+12%** (altcoins) above the coin's average cost, after fees.
- A trailing take-profit then follows the peak at **1.5%** (BTC/ETH) or **2.5%** (altcoins). When price drops that far from the peak, **100%** of the coin is sold.
- All of the money (stake + profit) returns to the pot for the next dip.
- There's no stop-loss: a coin that keeps falling stays in the portfolio.
