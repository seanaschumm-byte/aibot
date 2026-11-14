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





if __name__ == "__main__":
    main()
