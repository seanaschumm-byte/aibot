"""Which active bot would have made the most over the last 6 months? Replays each bot on real data
with its own deposit schedule, then ranks them by time-weighted return (deposits excluded).

  python compare_6m.py        (needs ALPACA_API_KEY / ALPACA_SECRET_KEY; runs in GitHub Actions)
Bots: $ Bot 5 (Smart Reserve), Sean Realistisch, Sean Lavarge (futures 10x and ~4x ETF version), and 'just buy SPY' / 'just buy BTC' with the same deposits as $ Bot 5 for reference.
"""
from __future__ import annotations

import datetime as dt
import math
import os

import pandas as pd
import requests

MONTHS = 6
EURUSD = 1.10
DATA = "https://data.alpaca.markets"
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "proxy-injected"),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "proxy-injected")}
END = dt.datetime.now(dt.timezone.utc)
START = END - dt.timedelta(days=int(MONTHS * 30.44))


def crypto_bars(coins: list[str], timeframe: str, start: dt.datetime) -> dict[str, pd.DataFrame]:
    params = {"symbols": ",".join(coins), "timeframe": timeframe, "start": start.isoformat(), "limit": 10000}
    raw: dict[str, list] = {c: [] for c in coins}
    while True:
        js = requests.get(DATA + "/v1beta3/crypto/us/bars", headers=H, params=params, timeout=60).json()
        for s, b in (js.get("bars") or {}).items():
            raw[s] += b
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    out = {}
    for s, b in raw.items():
        df = pd.DataFrame(b)
        if df.empty:
            continue
        df["t"] = pd.to_datetime(df["t"], utc=True)
        out[s] = df.set_index("t").rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return out


def twr_stats(curve: list[tuple]) -> tuple[float, float]:
    """(period return, max drawdown) from (time, equity, deposit_at_t) points."""
    eq = pd.DataFrame(curve, columns=["t", "e", "d"])
    prev = eq["e"].shift(1)
    r = ((eq["e"] - eq["d"] - prev) / prev).fillna(0)
    twr = (1 + r).cumprod()
    return float(twr.iloc[-1] - 1), float((twr / twr.cummax() - 1).min())


# ---------------------------------------------------------------- $ Bot 5 (Smart Reserve)
def smart_reserve() -> dict:
    from reserve_strategy import CAPS, Rules, check_buy, check_sell
    coins = list(CAPS)
    h1 = crypto_bars(coins, "1Hour", START - dt.timedelta(days=25))
    r, fee = Rules(), 0.0025
    idx = sorted(set().union(*[set(d.index) for d in h1.values()]))
    idx = [t for t in idx if t >= START]
    cash = 500 * EURUSD
    deposited = cash
    hold = {c: {"qty": 0.0, "cost": 0.0, "lbt": None, "lbp": None} for c in coins}
    curve, trades, wins, last_monday = [], 0, 0, None
    h4_all = {c: d.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
              for c, d in h1.items()}
    last_px: dict[str, float] = {}
    spy_like = {"btc_units": 0.0}                 # same deposits into BTC, for reference
    btc = h1["BTC/USD"]
    spy_like["btc_units"] = cash / btc.loc[btc.index >= START, "close"].iloc[0]
    for t in idx:
        dep = 0.0
        if t.weekday() == 0 and t.hour == 0 and t.normalize() != last_monday and t > idx[0]:
            last_monday = t.normalize()
            dep = 100 * EURUSD
            cash += dep
            deposited += dep
            if t in btc.index:
                spy_like["btc_units"] += dep / btc.loc[t, "close"]
        for c in coins:
            if t in h1[c].index:
                last_px[c] = float(h1[c].loc[t, "close"])
        prices = dict(last_px)
        for c, h in hold.items():                 # trailing take-profit
            if h["qty"] > 0 and c in prices:
                since = h1[c].loc[h["lbt"]:t]
                ok, _ = check_sell(c, h["cost"] / h["qty"], prices[c],
                                   [{"h": x} for x in since["high"]], r)
                if ok:
                    got = h["qty"] * prices[c] * (1 - fee)
                    trades += 1
                    wins += got > h["cost"]
                    cash += got
                    hold[c] = {"qty": 0.0, "cost": 0.0, "lbt": None, "lbp": None}
        pot = cash + sum(h["qty"] * prices.get(c, 0) for c, h in hold.items())
        order = 25 * EURUSD
        if cash >= order:
            cands = []
            for c in coins:
                if c not in prices:
                    continue
                hist = h1[c].loc[:t].iloc[-170:]
                if len(hist) < 168:
                    continue
                h4 = h4_all[c].loc[:t - pd.Timedelta(hours=3)].iloc[-60:]       # completed 4h bars only
                hrs = (t - hold[c]["lbt"]).total_seconds() / 3600 if hold[c]["lbt"] is not None else None
                # approximation: the 15-min SMA9 'not in free fall' check uses the last 9 hourly closes
                ok, _, rs = check_buy(c, prices[c], [{"h": x, "o": o, "c": cl} for x, o, cl in
                                                     zip(hist["high"], hist["open"], hist["close"])],
                                      [{"c": x} for x in h4["close"]], [{"c": x} for x in hist["close"].iloc[-9:]],
                                      hold[c]["qty"] * prices[c], pot, order, hrs, hold[c]["lbp"], r)
                if ok:
                    cands.append((rs, c))
            if cands:
                _, c = min(cands)
                cash -= order
                hold[c]["qty"] += order / prices[c] * (1 - fee)
                hold[c]["cost"] += order
                hold[c]["lbt"], hold[c]["lbp"] = t, prices[c]
        equity = cash + sum(h["qty"] * prices.get(c, 0) for c, h in hold.items())
        curve.append((t, equity, dep))
    ret, dd = twr_stats(curve)
    final = curve[-1][1]
    btc_final = spy_like["btc_units"] * float(btc["close"].iloc[-1])
    return {"name": "$ Bot 5 - Smart Reserve", "deposited": deposited / EURUSD, "final": final / EURUSD,
            "ret": ret, "dd": dd, "trades": trades, "note": f"{wins}/{trades} sales in profit; still holding "
            f"EUR {(final - cash) / EURUSD:.0f} in coins",
            "ref": ("Same deposits into BTC", btc_final / EURUSD)}


# ---------------------------------------------------------------- Sean Realistisch
def sean_realistisch() -> dict:
    from sean_realistisch import config as SC
    from sean_realistisch.backtest import download, simulate
    m = simulate(download(MONTHS), MONTHS)
    ret = (1 + m["cagr"]) ** m["years"] - 1
    spy_ret = (1 + m["spy_cagr"]) ** m["years"] - 1
    return {"name": "Sean Realistisch", "deposited": m["deposited"] / EURUSD, "final": m["final_equity"] / EURUSD,
            "ret": ret, "dd": m["max_drawdown"], "trades": m["trades"],
            "note": f"win rate {m['win_rate']:.0%}; just buying SPY: {spy_ret:+.1%}",
            "ref": ("Same deposits into SPY", m["spy_final"] / EURUSD)}


# ---------------------------------------------------------------- Sean Lavarge
def sean_lavarge(max_lev: int) -> dict:
    os.environ["MAX_LEVERAGE"] = str(max_lev)
    import importlib
    from sean_lavarge import config as LC
    importlib.reload(LC)
    from sean_lavarge import risk_manager, strategy, backtest
    for mod in (risk_manager, strategy, backtest):
        importlib.reload(mod)
    data = {s: backtest.download(s, int(MONTHS * 30.44) + 40) for s in LC.BACKTEST_SYMBOLS}
    m = backtest.simulate(data)
    label = "futures, max 10x" if max_lev == 10 else "Alpaca ETF version, ~4x"
    return {"name": f"Sean Lavarge ({label})", "deposited": m["deposited"], "final": m["final_equity"],
            "ret": m["twr_return"], "dd": m["max_drawdown"], "trades": m["trades"],
            "note": f"win rate {m['win_rate']:.0%}, {m['breaker_pauses']} breaker pauses"}


def main() -> None:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    results = []
    for fn, args in ((smart_reserve, ()), (sean_realistisch, ()), (sean_lavarge, (4,)), (sean_lavarge, (10,))):
        try:
            results.append(fn(*args))
            print("done:", results[-1]["name"], flush=True)
        except Exception as e:
            print(f"FAILED {fn.__name__}{args}: {type(e).__name__}: {e}", flush=True)
    results.sort(key=lambda r: r["ret"], reverse=True)
    lines = [f"## Last {MONTHS} months: {START:%Y-%m-%d} to {END:%Y-%m-%d} (EUR, approx. EURUSD {EURUSD})", "",
             "| # | Bot | Deposited | End value | Profit | Return (excl. deposits) | Max drawdown | Trades | Notes |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        dd = "n/a" if math.isnan(r["dd"]) else f"{r['dd']:.1%}"
        lines.append(f"| {i} | {r['name']} | EUR {r['deposited']:,.0f} | EUR {r['final']:,.0f} | "
                     f"EUR {r['final'] - r['deposited']:+,.0f} | **{r['ret']:+.1%}** | {dd} | {r['trades']} | {r['note']} |")
    refs = [f"{r['ref'][0]}: EUR {r['ref'][1]:,.0f} (deposited EUR {r['deposited']:,.0f})" for r in results if r.get("ref")]
    lines += ["", "Reference: " + "; ".join(refs)]
    text = "\n".join(lines)
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
