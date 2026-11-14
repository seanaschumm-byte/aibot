"""Connectivity check (no API keys needed): can we reach the exchange (demo if TESTNET) and do
the configured perpetual markets exist with 4h candles?"""
from . import config as C
import ccxt


def main() -> None:
    ex = getattr(ccxt, C.EXCHANGE_ID)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    if C.TESTNET:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    print(f"{C.EXCHANGE_ID} {'DEMO' if C.TESTNET else 'LIVE'}: {len(ex.markets)} markets")
    for s in C.SYMBOLS:
        m = ex.markets.get(s)
        if not m:
            print(f"  {s}: NOT FOUND")
            continue
        rows = ex.fetch_ohlcv(s, C.TIMEFRAME, None, 300)
        print(f"  {s}: id {m['id']}, contract size {m.get('contractSize')}, min amount "
              f"{(m.get('limits') or {}).get('amount', {}).get('min')}, {len(rows)} x {C.TIMEFRAME} candles, "
              f"last close {rows[-1][4] if rows else '-'}")




def diagnose() -> None:
    """Why would the demo have no markets? Compare raw demo instruments with live."""
    import json
    for sandbox in (True, False):
        ex = getattr(ccxt, C.EXCHANGE_ID)({"enableRateLimit": True})
        if sandbox:
            ex.set_sandbox_mode(True)
        try:
            raw = ex.publicGetInstruments()
            if isinstance(raw, str):
                print(f"[{'demo' if sandbox else 'live'}] NON-JSON answer ({len(raw)} chars): {raw[:400]!r}")
                import requests
                for url in ("https://demo-futures.kraken.com/derivatives/api/v3/instruments",
                            "https://demo-futures.kraken.com/derivatives/api/v3/tickers"):
                    r = requests.get(url, timeout=15)
                    print(f"   GET {url} -> {r.status_code} {r.headers.get('content-type')} {r.text[:200]!r}")
                continue
            inst = raw.get("instruments", [])
            pf = [i for i in inst if str(i.get("symbol", "")).upper().startswith("PF_")]
            print(f"[{'demo' if sandbox else 'live'}] raw instruments: {len(inst)}, PF_*: {len(pf)}, "
                  f"result={raw.get('result')}, keys={list(raw)[:5]}")
            for i in pf[:3]:
                print("   ", json.dumps({k: i.get(k) for k in ('symbol', 'type', 'tradeable', 'base', 'quote', 'contractSize')}))
            ex.load_markets(True)
            print(f"   ccxt markets: {len(ex.markets)}; sample: {[s for s in ex.symbols if 'BTC' in s][:6]}")
        except Exception as e:
            print(f"[{'demo' if sandbox else 'live'}] ERROR {type(e).__name__}: {str(e)[:300]}")


def sandboxes() -> None:
    """Which demo/testnet exchanges answer from this machine?"""
    for eid, sym in [("bybit", "BTC/USDT:USDT"), ("binanceusdm", "BTC/USDT:USDT"), ("okx", "BTC/USDT:USDT"),
                     ("bitget", "BTC/USDT:USDT"), ("bitmex", "BTC/USDT:USDT"), ("krakenfutures", "BTC/USD:USD")]:
        try:
            ex = getattr(ccxt, eid)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
            ex.set_sandbox_mode(True)
            ex.load_markets()
            ok = sym in ex.markets
            n = len(ex.fetch_ohlcv(sym, "4h", None, 50)) if ok else 0
            print(f"SANDBOX {eid}: {len(ex.markets)} markets, {sym} {'OK' if ok else 'missing'}, {n} candles")
        except Exception as e:
            print(f"SANDBOX {eid}: ERROR {type(e).__name__}: {str(e)[:160]}")


if __name__ == "__main__":
    sandboxes()
    main()
