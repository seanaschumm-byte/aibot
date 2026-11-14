"""Botsi: the MACD Nasdaq rules on crypto (BTC, ETH, SOL), 24/7.

EUR 500 at the start + EUR 100 every week, split equally over the three coins (each its own sleeve).
Per coin: buy on a MACD(12,26,9) buy signal or a 12% dip under its 3-month high; sell 50% of a
buy at +12%, the rest at +25% (or at +18% if it falls back after passing it); MACD sell signal
sells everything of that coin. Money from sales goes into that coin's next buy.
Orders: client_order_id "bs-<COIN>-<B|S>-<ms>"; lots in botsi/state/state.json.
  python -m botsi.bot            (DRY_RUN=1 to only log)
"""
from __future__ import annotations

import logging
import os

from macd_nasdaq import bot as engine

engine.NAME, engine.PREFIX = "Botsi", "bs"
engine.MARKETS = {
    "BTC": ("BTC/USD", "crypto", 1 / 3, 90),
    "ETH": ("ETH/USD", "crypto", 1 / 3, 90),
    "SOL": ("SOL/USD", "crypto", 1 / 3, 90),
}
engine.STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "state.json")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    engine.run_once()


if __name__ == "__main__":
    main()
