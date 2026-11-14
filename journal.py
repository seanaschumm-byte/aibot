"""Trade journal: one JSON line per event in journal/<bot>.jsonl, committed to the repo
by the GitHub workflow after each run. The weekly review reads it to learn what works.

Events:
  entry  - a buy, with the reasons/conditions at that moment (features + market mood)
  close  - a finished trade, with profit in $ and %
  week   - the weekly dip bot's weekly market prices and order levels
  market - one market-mood snapshot per day
"""
from __future__ import annotations

import datetime as dt
import json
import os

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal")


def _path(bot: str) -> str:
    return os.path.join(DIR, f"{bot}.jsonl")


def read(bot: str) -> list[dict]:
    try:
        with open(_path(bot)) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def write(bot: str, event: str, **data) -> None:
    if os.environ.get("DRY_RUN") == "1":
        print(f"[journal dry run] {bot} {event} {data}")
        return
    os.makedirs(DIR, exist_ok=True)
    rec = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "event": event, **data}
    with open(_path(bot), "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def has(bot: str, event: str, key: str, value) -> bool:
    return any(r.get("event") == event and r.get(key) == value for r in read(bot))


def record_closes(bot: str, closes: list[dict]) -> None:
    """closes: dicts with at least 'cid'. Each is written only once."""
    done = {r.get("cid") for r in read(bot) if r.get("event") == "close"}
    for c in closes:
        if c["cid"] not in done:
            write(bot, "close", **c)


def daily_market(bot: str, m: dict) -> None:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    if not any(r.get("event") == "market" and r.get("time", "")[:10] == today for r in read(bot)):
        write(bot, "market", mood=m)
