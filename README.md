# Trading bots (Alpaca paper)

**📊 Check each bot separately: [STATUS.md](STATUS.md)**, refreshed every hour.

The top 3 of the 6-month replay (16 May – 14 Nov 2026, `compare_6m.py`). All of them trade on the **Alpaca paper account**:

| Bot | What it does | 6-month replay | Order label |
|---|---|---|---|
| **Sean Lavarge** ([`sean_lavarge/`](sean_lavarge/README.md)) | 4h Turtle breakouts on BTC/ETH/SOL, traded with 2× / inverse crypto ETFs (BITU, ETHT, SOLZ / SBIT, ETHD), about 4× max, long and short. €500 + €150/week. | +26.0%, max drawdown −27.5% | `sl-` |
| **$ Bot 5 - Smart Reserve** (`reserve_bot.py`) | Buys €25 dips on 7 coins from one euro pot (€500 + €100/week), lowest RSI first, with a cap per coin and a trailing take-profit. | +0.9%, max drawdown −10.7% (BTC: −7.7%) | `rsv-` |
| **Sean Realistisch** ([`sean_realistisch/`](sean_realistisch/README.md)) | Trend following on SPY/QQQ (2× SSO/QLD in a bull market), gold (GLD) and bonds (TLT, IEF). $500 + $300/month, no margin. | −1.4%, max drawdown −6.9% (SPY: +0.5%) | `sr-` |

| **MACD Nasdaq** ([`macd_nasdaq/`](macd_nasdaq/strategy.py)) | Winner of the 8-month test of 14 known strategies. QQQ in when MACD(12,26,9) is above its signal line, and also buys 12% dips (vs the 3-month high). Each buy: 50% sold at +12%, the rest at +25%, or at +18% if it falls back after passing it. MACD sell signal sells all. Money from sales goes into the next buy. €500 + €100/week. | added 14 Nov 2026 | `mn-` |

Also in the account: **5 manual crypto trades** (`manual25-`) with a +30% take-profit and a −40% stop-loss guard (`manual_guard.py`, every 15 min).

Removed on 14 Nov 2026: Bots 1–4 (stocks VWAP dip, BTC risk, crypto daily dip, weekly dip) and Market Making. They are still in the git history.

## Shared files

| File | What it does |
|---|---|
| `status.py` | Builds `STATUS.md`: profit/loss per bot from its order label (hourly workflow) |
| `market.py` | Market mood: Crypto Fear & Greed index, BTC and SPY vs their 50-day average |
| `journal.py` | Trade journal (`journal/*.jsonl`), read by the Saturday review |
| `common.py` | Alpaca REST helpers used by $ Bot 5 |
| `compare_6m.py` | Replays the bots on the last 6 months of real data |

## Learning loop

Every Saturday a scheduled Claude review reads the journals, the bot state files and Alpaca's order history. It explains losing trades, looks for patterns and proposes at most 3 small rule changes. **Nothing changes unless you approve it.** Patterns only mean something after about 30 trades per bot.

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
