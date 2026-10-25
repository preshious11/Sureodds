"""SportyBet football 1X2 from the public catalogue (read-only GET, no booking calls)."""
from __future__ import annotations

import json
import os
from typing import Dict, List

from predbot.pro_value.oddsapi_raw import stamp


def parse_sporty_1x2(data) -> List[Dict]:
    """Events with a 1X2 market, in the shape the matcher expects. ``data`` is the decoded catalogue body."""
    out = []
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    for tt in (body or {}).get("tournaments", []):
        for e in tt.get("events", []):
            x = next((m for m in e.get("markets", []) if m.get("id") == "1"), None)
            if not x:
                continue
            odds = {o.get("desc"): float(o["odds"]) for o in x.get("outcomes", []) if o.get("isActive", 1)}
            if {"Home", "Draw", "Away"} <= set(odds):
                out.append({"event_id": e.get("eventId"), "home_team": e.get("homeTeamName"),
                            "away_team": e.get("awayTeamName"),
                            "kickoff_ts": int(e["estimateStartTime"]) // 1000, "odds": odds})
    return out


def fetch_sporty_1x2(transport, raw_dir: str, now_ts: int, pages: int = 2) -> List[Dict]:
    """Pull the football 1X2 catalogue (pages of 100). Each page is written to disk before parsing."""
    from predbot.sporty_booking import envelope
    os.makedirs(raw_dir, exist_ok=True)
    events: List[Dict] = []
    for page in range(1, pages + 1):
        data = transport.request("GET", "/factsCenter/pcUpcomingEvents", params={
            "sportId": "sr:sport:1", "marketId": "1", "pageSize": 100, "pageNum": page,
            "todayGames": "false", "timeline": 72})
        path = os.path.join(raw_dir, f"sportybet_1x2__{stamp(now_ts)}__p{page}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)                                             # saved before parsing
        events.extend(parse_sporty_1x2(envelope(data)))
    for e in events:
        e["fetched_at"] = now_ts
    return events
