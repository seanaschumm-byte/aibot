"""Which well-known trading strategy worked best over the last 8 months?
EUR 500 start + EUR 100 every week (first trading day of the week), real daily Alpaca data.
Orders are executed at the next day's open (no look-ahead); 0.05% slippage, 0.25% fee on crypto.
Ranked by time-weighted return (deposits don't count as profit).
  python compare_strategies.py      (needs ALPACA keys; runs in GitHub Actions)
"""
from __future__ import annotations

import datetime as dt
import math
import os

import numpy as np
import pandas as pd
import requests

EURUSD, MONTHS = 1.10, float(os.getenv("MONTHS", "8"))
START_EUR, WEEKLY_EUR = 500.0, 100.0
DATA = "https://data.alpaca.markets"
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}
END = pd.Timestamp.now(tz="UTC").normalize()
START = END - pd.Timedelta(days=int(MONTHS * 30.44))
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
STOCKS = ["SPY", "QQQ", "EFA", "IEF", "TLT", "GLD"] + SECTORS


def get(path: str, params: dict, key: str) -> dict[str, list]:
    out: dict[str, list] = {}
    while True:
        js = requests.get(DATA + path, headers=H, params=params, timeout=60).json()
        for s, b in (js.get(key) or {}).items():
            out.setdefault(s, []).extend(b)
        if not js.get("next_page_token"):
            return out
        params = {**params, "page_token": js["next_page_token"]}


def frames(raw: dict[str, list]) -> dict[str, pd.DataFrame]:
    res = {}
    for s, b in raw.items():
        df = pd.DataFrame(b)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.normalize()
        res[s] = df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return res


def load() -> dict[str, pd.DataFrame]:
    first = (START - pd.Timedelta(days=420)).isoformat()
    data = frames(get("/v2/stocks/bars", {"symbols": ",".join(STOCKS), "timeframe": "1Day", "start": first,
                                          "limit": 10000, "feed": "iex", "adjustment": "all"}, "bars"))
    data.update(frames(get("/v1beta3/crypto/us/bars", {"symbols": "BTC/USD", "timeframe": "1Day", "start": first,
                                                       "limit": 10000}, "bars")))
    return data


# ---------------------------------------------------------------- indicators
def sma(s, n): return s.rolling(n).mean()


def rsi(s, n):
    d = s.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def ret(s, n): return s / s.shift(n) - 1


# ---------------------------------------------------------------- strategies: target weights from data up to day t
def hold(sym):
    return lambda d, t, cur: {sym: 1.0}


def faber(d, t, cur):                  # Meb Faber: SPY above its 200-day SMA, else cash
    c = d["SPY"]["close"].loc[:t]
    return {"SPY": 1.0} if c.iloc[-1] > sma(c, 200).iloc[-1] else {}


def golden(d, t, cur):                 # golden cross 50/200 on SPY
    c = d["SPY"]["close"].loc[:t]
    return {"SPY": 1.0} if sma(c, 50).iloc[-1] > sma(c, 200).iloc[-1] else {}


def gem(d, t, cur):                    # Antonacci Global Equities Momentum, monthly
    if cur is not None and not month_start(d, t):
        return cur
    r = {s: ret(d[s]["close"].loc[:t], 252).iloc[-1] for s in ("SPY", "EFA", "IEF")}
    if r["SPY"] > 0.04:                # beats ~T-bill yield
        return {"SPY": 1.0} if r["SPY"] >= r["EFA"] else {"EFA": 1.0}
    return {"IEF": 1.0}


def sectors(d, t, cur):                # sector momentum: top 3 by 3-month return, monthly
    if cur is not None and not month_start(d, t):
        return cur
    r = sorted(((ret(d[s]["close"].loc[:t], 63).iloc[-1], s) for s in SECTORS if s in d), reverse=True)[:3]
    return {s: 1 / 3 for _, s in r}


def connors(d, t, cur):                # Larry Connors RSI(2): buy dips above the 200 SMA, sell above the 5 SMA
    c = d["SPY"]["close"].loc[:t]
    if cur:
        return {} if c.iloc[-1] > sma(c, 5).iloc[-1] else cur
    return {"SPY": 1.0} if c.iloc[-1] > sma(c, 200).iloc[-1] and rsi(c, 2).iloc[-1] < 10 else {}


def bollinger(d, t, cur):              # buy under the lower band, sell at the middle
    c = d["SPY"]["close"].loc[:t]
    mid, sd = sma(c, 20).iloc[-1], c.rolling(20).std().iloc[-1]
    if cur:
        return {} if c.iloc[-1] >= mid else cur
    return {"SPY": 1.0} if c.iloc[-1] < mid - 2 * sd else {}


def macd(d, t, cur):                   # MACD(12,26,9) crossover on QQQ
    c = d["QQQ"]["close"].loc[:t]
    m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    return {"QQQ": 1.0} if m.iloc[-1] > m.ewm(span=9, adjust=False).mean().iloc[-1] else {}


def all_weather(d, t, cur):            # Harry Browne permanent portfolio style, monthly rebalance
    return {"SPY": 0.25, "TLT": 0.25, "GLD": 0.25, "IEF": 0.25}


def turtle_btc(d, t, cur):             # Turtle breakout on BTC daily: 20-day high in, 10-day low out
    b = d["BTC/USD"].loc[:t]
    if cur:
        return {} if b["close"].iloc[-1] < b["low"].iloc[-11:-1].min() else cur
    return {"BTC/USD": 1.0} if b["close"].iloc[-1] > b["high"].iloc[-21:-1].max() else {}


def btc_trend(d, t, cur):              # BTC above its 200-day SMA
    c = d["BTC/USD"]["close"].loc[:t]
    return {"BTC/USD": 1.0} if c.iloc[-1] > sma(c, 200).iloc[-1] else {}


def month_start(d, t):
    idx = d["SPY"].index
    i = idx.get_loc(t)
    return i == 0 or idx[i - 1].month != t.month


STRATEGIES = [
    ("Buy every week: S&P 500 (SPY)", hold("SPY"), "stocks", True),
    ("Buy every week: Nasdaq-100 (QQQ)", hold("QQQ"), "stocks", True),
    ("Buy every week: gold (GLD)", hold("GLD"), "stocks", True),
    ("Buy every week: bitcoin", hold("BTC/USD"), "crypto", True),
    ("Meb Faber 200-day trend (SPY / cash)", faber, "stocks", False),
    ("Golden cross 50/200 (SPY / cash)", golden, "stocks", False),
    ("Dual Momentum GEM (Antonacci)", gem, "stocks", False),
    ("Sector momentum top 3 (monthly)", sectors, "stocks", False),
    ("Connors RSI(2) pullbacks (SPY)", connors, "stocks", False),
    ("Bollinger Band mean reversion (SPY)", bollinger, "stocks", False),
    ("MACD crossover (QQQ)", macd, "stocks", False),
    ("Permanent portfolio (SPY/TLT/GLD/IEF)", all_weather, "stocks", True),
    ("Turtle breakout (bitcoin, daily)", turtle_btc, "crypto", False),
    ("Bitcoin 200-day trend", btc_trend, "crypto", False),
]


def simulate(d, fn, cal, static) -> dict:
    days = d["SPY"].index if cal == "stocks" else d["BTC/USD"].index
    days = [t for t in days if t >= START]
    cash, units, deposited, curve, trades, target, week = START_EUR * EURUSD, {}, START_EUR, [], 0, None, None
    pending = None
    for i, t in enumerate(days):
        dep = 0.0
        wk = t.isocalendar()[:2]
        if i > 0 and wk != week:
            dep = WEEKLY_EUR * EURUSD
            cash += dep
            deposited += WEEKLY_EUR
        week = wk
        px_open = {s: float(d[s].loc[t, "open"]) for s in d if t in d[s].index}
        # execute yesterday's decision (or invest new deposits / monthly rebalance) at today's open
        if pending is not None or dep or (static and month_start(d, t) if cal == "stocks" else False):
            tgt = pending if pending is not None else (target or {})
            equity = cash + sum(q * px_open.get(s, 0) for s, q in units.items())
            new = {s: equity * w / px_open[s] * (1 - 0.0005) for s, w in tgt.items() if s in px_open}
            changed = set(new) != set(units)
            for s in set(units) | set(new):
                diff = (new.get(s, 0) - units.get(s, 0)) * px_open.get(s, 0)
                fee = abs(diff) * (0.0025 if "/" in s else 0.0)
                cash -= diff + fee
            trades += changed
            units = {s: q for s, q in new.items() if q > 0}
            pending = None
        equity = cash + sum(q * float(d[s].loc[t, "close"]) for s, q in units.items() if t in d[s].index)
        curve.append((t, equity, dep))
        new_target = fn(d, t, target)
        if new_target != target:
            pending, target = new_target, new_target
    eq = pd.DataFrame(curve, columns=["t", "e", "d"])
    prev = eq["e"].shift(1)
    r = ((eq["e"] - eq["d"] - prev) / prev).fillna(0)
    twr = (1 + r).cumprod()
    ann = 252 if cal == "stocks" else 365
    return {"final": curve[-1][1] / EURUSD, "deposited": deposited, "ret": float(twr.iloc[-1] - 1),
            "dd": float((twr / twr.cummax() - 1).min()), "sharpe": float(r.mean() / r.std() * math.sqrt(ann)) if r.std() else 0,
            "switches": trades, "invested_now": bool(units)}


def main() -> None:
    d = load()
    rows = []
    for name, fn, cal, static in STRATEGIES:
        try:
            rows.append((name, simulate(d, fn, cal, static)))
        except Exception as e:
            print(f"FAILED {name}: {type(e).__name__}: {e}")
    rows.sort(key=lambda x: x[1]["ret"], reverse=True)
    lines = [f"## Known strategies, {START:%Y-%m-%d} to {END:%Y-%m-%d}: EUR 500 start + EUR 100/week", "",
             "| # | Strategy | Deposited | End value | Profit | Return (excl. deposits) | Max drawdown | Sharpe | Trades | In the market now |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, (n, r) in enumerate(rows, 1):
        lines.append(f"| {i} | {n} | EUR {r['deposited']:,.0f} | EUR {r['final']:,.0f} | EUR {r['final'] - r['deposited']:+,.0f} | "
                     f"**{r['ret']:+.1%}** | {r['dd']:.1%} | {r['sharpe']:.2f} | {r['switches']} | {'yes' if r['invested_now'] else 'cash'} |")
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
