"""Shared Alpaca helpers for $ Bot 5 (moved here from the removed Bot 1)."""
from __future__ import annotations

import os
import time

import requests

TRADE_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
EURUSD_FALLBACK = 1.15                  # used if the live FX lookup fails

S = requests.Session()
S.headers.update({
    "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", "proxy-injected"),
    "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", "proxy-injected"),
})


def api(method: str, path: str, base: str = TRADE_URL, **kw):
    """Alpaca REST call with retries on rate limits and server errors."""
    for attempt in range(3):
        r = S.request(method, base + path, timeout=20, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else None
    r.raise_for_status()


def eurusd() -> float:
    if os.environ.get("EURUSD"):
        return float(os.environ["EURUSD"])
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=EUR&to=USD", timeout=10)
        return float(r.json()["rates"]["USD"])
    except Exception:
        return EURUSD_FALLBACK
