"""Which buy-the-dip strategy worked best over the last 6 months? Portfolio replay for Justin
Tradingbot: EUR 500 start + EUR 100 every week, max 5 positions (3 for crypto), each up to 1/5 of
equity, signals on the daily close -> orders at the next open, 0.05% slippage (+0.25% fee on crypto).
  python -m justin.backtest [months]
"""
from __future__ import annotations

import math
import os
import sys

import pandas as pd
import requests

from .strategies import STRATEGIES, indicators

EURUSD = 1.10
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}
STOCKS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA",
          "JPM", "XLE", "GLD"]
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD"]


def load(symbols: list[str], months: float) -> dict[str, pd.DataFrame]:
    crypto = "/" in symbols[0]
    start = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(months * 30.44) + 320)).isoformat()
    url = "https://data.alpaca.markets/" + ("v1beta3/crypto/us/bars" if crypto else "v2/stocks/bars")
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "limit": 10000}
    if not crypto:
        params.update({"feed": "iex", "adjustment": "all"})
    raw: dict[str, list] = {}
    while True:
        js = requests.get(url, headers=H, params=params, timeout=60).json()
        for s, b in (js.get("bars") or {}).items():
            raw.setdefault(s, []).extend(b)
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    out = {}
    for s, b in raw.items():
        df = pd.DataFrame(b)
        df["t"] = pd.to_datetime(df["t"], utc=True).dt.normalize()
        out[s] = indicators(df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close",
                                                               "v": "volume"}))
    return out


def simulate(data: dict[str, pd.DataFrame], name: str, start: pd.Timestamp, max_pos: int, fee: float) -> dict:
    days = sorted(t for t in set().union(*[set(d.index) for d in data.values()]) if t >= start)
    slip = 0.0005 + fee
    cash, deposited, pos, curve, trades, week = 500 * EURUSD, 500.0, {}, [], [], None
    pend_exit: list[str] = []
    pend_entry: list[str] = []
    strat = STRATEGIES.get(name)
    for i, t in enumerate(days):
        dep = 0.0
        if i > 0 and t.isocalendar()[:2] != week:
            dep = 100 * EURUSD
            cash += dep
            deposited += 100
        week = t.isocalendar()[:2]
        bar = {s: d.loc[t] for s, d in data.items() if t in d.index}
        # orders at the open
        for s in pend_exit:
            if s in pos and s in bar:
                p = pos.pop(s)
                px = bar[s]["open"] * (1 - slip)
                cash += p["qty"] * px
                trades.append(px / p["entry"] - 1)
        equity = cash + sum(p["qty"] * bar[s]["open"] for s, p in pos.items() if s in bar)
        if strat is None:                                    # benchmark: buy the basket every week
            if dep or i == 0:
                each = cash / len(bar)
                for s in bar:
                    q = each / (bar[s]["open"] * (1 + slip))
                    old = pos.get(s, {"qty": 0.0, "entry": 0.0, "days": 0})
                    pos[s] = {"qty": old["qty"] + q, "entry": bar[s]["open"], "days": 0}
                cash = 0.0
        else:
            for s in pend_entry:
                if s in pos or s not in bar or len(pos) >= max_pos:
                    continue
                usd = min(cash, equity / max_pos)
                if usd < 1:
                    break
                px = bar[s]["open"] * (1 + slip)
                cash -= usd
                pos[s] = {"qty": usd / px, "entry": px, "days": 0}
        pend_exit, pend_entry = [], []
        equity = cash + sum(p["qty"] * bar[s]["close"] for s, p in pos.items() if s in bar)
        curve.append((t, equity, dep))
        if strat is None:
            continue
        # signals on the close
        for s, p in pos.items():
            if s in bar:
                p["days"] += 1
                if strat["exit"](bar[s], p):
                    pend_exit.append(s)
        free = max_pos - len(pos) + len(pend_exit)
        cands = sorted((strat["rank"](bar[s]), s) for s in bar if s not in pos and strat["entry"](bar[s]))
        pend_entry = [s for _, s in cands[:max(free, 0)]]
    eq = pd.DataFrame(curve, columns=["t", "e", "d"])
    prev = eq["e"].shift(1)
    r = ((eq["e"] - eq["d"] - prev) / prev).fillna(0)
    twr = (1 + r).cumprod()
    return {"name": name, "deposited": deposited, "final": curve[-1][1] / EURUSD, "ret": float(twr.iloc[-1] - 1),
            "dd": float((twr / twr.cummax() - 1).min()), "trades": len(trades),
            "win": sum(x > 0 for x in trades) / len(trades) if trades else 0.0,
            "avg": sum(trades) / len(trades) if trades else 0.0,
            "sharpe": float(r.mean() / r.std() * math.sqrt(252)) if r.std() else 0.0}


def run(group: str, symbols: list[str], months: float, max_pos: int, fee: float) -> list[dict]:
    data = load(symbols, months)
    end = min(d.index[-1] for d in data.values())
    data = {s: d.loc[:end] for s, d in data.items()}
    start = end - pd.Timedelta(days=int(months * 30.44))
    res = [simulate(data, n, start, max_pos, fee) for n in STRATEGIES]
    res.append(simulate(data, "Benchmark: buy all every week", start, max_pos, fee))
    for r in res:
        r["group"] = group
    return res


def main() -> None:
    months = float(sys.argv[1]) if len(sys.argv) > 1 else 6
    rows = run("Stocks/ETFs", STOCKS, months, 5, 0.0) + run("Crypto", CRYPTO, months, 3, 0.0025)
    rows.sort(key=lambda r: r["final"] - r["deposited"], reverse=True)
    lines = [f"## Justin Tradingbot: buy-the-dip strategies, last {months:g} months, EUR 500 + EUR 100/week", "",
             "| # | Strategy | Market | Deposited | End value | **Profit** | Return (excl. deposits) | Max drawdown | Trades | Win rate | Avg trade |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        lines.append(f"| {i} | {r['name']} | {r['group']} | EUR {r['deposited']:,.0f} | EUR {r['final']:,.0f} | "
                     f"**EUR {r['final'] - r['deposited']:+,.0f}** | {r['ret']:+.1%} | {r['dd']:.1%} | {r['trades']} | "
                     f"{r['win']:.0%} | {r['avg']:+.2%} |")
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
