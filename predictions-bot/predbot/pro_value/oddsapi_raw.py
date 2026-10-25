"""OddsAPI reading for the shadow feature: raw files saved to disk before any parsing, and a league-level 1X2 client.

Raw file names carry the UTC fetch time: ``<league>__<YYYYmmddTHHMMSSZ>.json``. Parsing never runs on bytes that
have not already been written to disk."""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, List, Optional

from predbot.pro_value.credits import CreditGuard

API = "https://api.the-odds-api.com/v4"


def stamp(now_ts: int) -> str:
    return dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_raw(raw_dir: str, league: str, body: bytes, now_ts: int) -> str:
    """Write the response bytes to a new file first. Refuses to overwrite an existing file."""
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, f"{league}__{stamp(now_ts)}.json")
    if os.path.exists(path):
        raise FileExistsError(path)
    with open(path, "wb") as f:
        f.write(body)
    return path


def _fetched_ts(path: str) -> int:
    text = os.path.basename(path).rsplit("__", 1)[-1].replace(".json", "")
    return int(dt.datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp())


def parse_1x2_events(body, league: str, fetched_at: int) -> List[Dict]:
    """Keep the h2h (1X2) market per bookmaker. Returns one dict per event."""
    out = []
    for ev in body if isinstance(body, list) else []:
        books: Dict[str, Dict[str, float]] = {}
        for b in ev.get("bookmakers", []):
            m = next((m for m in b.get("markets", []) if m.get("key") == "h2h"), None)
            if not m:
                continue
            prices = {}
            for o in m.get("outcomes", []):
                name = o.get("name")
                if name == ev.get("home_team"):
                    prices["Home"] = float(o["price"])
                elif name == ev.get("away_team"):
                    prices["Away"] = float(o["price"])
                elif name == "Draw":
                    prices["Draw"] = float(o["price"])
            if {"Home", "Draw", "Away"} <= set(prices):
                books[b["key"]] = prices
        kick = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        out.append({"id": ev.get("id"), "sport_key": league, "home_team": ev.get("home_team"),
                    "away_team": ev.get("away_team"), "commence_ts": int(kick.timestamp()),
                    "bookmakers": books, "fetched_at": fetched_at})
    return out


def load_raw_dir(raw_dir: str) -> List[Dict]:
    """Parse every saved league file in the directory. Parsing only ever reads files already on disk."""
    events: List[Dict] = []
    if not raw_dir or not os.path.isdir(raw_dir):
        return events
    for name in sorted(os.listdir(raw_dir)):
        if not name.endswith(".json") or "__" not in name:
            continue
        path = os.path.join(raw_dir, name)
        league = name.rsplit("__", 1)[0]
        with open(path, "rb") as f:
            body = json.loads(f.read().decode("utf-8"))
        events.extend(parse_1x2_events(body, league, _fetched_ts(path)))
    return events


class OddsApiClient:
    """Single-league 1X2 fetch (h2h, eu). Writes the raw response before parsing and respects the guard."""

    def __init__(self, api_key: str, raw_dir: str, guard: CreditGuard, regions: str = "eu", session=None):
        self.api_key, self.raw_dir, self.guard, self.regions = api_key, raw_dir, guard, regions
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    def fetch_league_h2h(self, league: str, now_ts: int) -> Optional[List[Dict]]:
        """One call = 1 credit (1 region x 1 market). Returns None and spends nothing when the guard refuses."""
        if not self.guard.can_spend(1):
            return None
        r = self.session.get(f"{API}/sports/{league}/odds",
                             params={"apiKey": self.api_key, "regions": self.regions, "markets": "h2h",
                                     "oddsFormat": "decimal"}, timeout=45)
        path = save_raw(self.raw_dir, league, r.content, now_ts)          # raw bytes on disk first
        self.guard.observe(r.headers.get("x-requests-remaining"))
        if r.status_code != 200:
            raise RuntimeError(f"OddsAPI status {r.status_code} for {league}")
        self.guard.spend(1)
        with open(path, "rb") as f:
            return parse_1x2_events(json.loads(f.read().decode("utf-8")), league, now_ts)
