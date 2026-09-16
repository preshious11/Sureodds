"""SharpAPI (https://docs.sharpapi.io) - GET /api/v1/odds rows.

Free tier: 12 req/min, DraftKings + FanDuel, 60 s delay, all sports.
SharpAPI has no results endpoint on the free tier (game state is a paid
add-on), so picks sourced here are settled through the cross-provider
results chain (SportsGameOdds / API-Football) by team names + kick-off.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import BaseProvider, Event, MarketLine
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.odds import parse_decimal
from predbot.util.timeutil import parse_iso

log = logging.getLogger(__name__)

SPORT_MAP = {
    "soccer": "football", "football": "american_football", "basketball": "basketball",
    "hockey": "hockey", "baseball": "baseball", "tennis": "tennis", "mma": "mma",
    "handball": "handball",
}

SEGMENT_PERIOD = {
    "full_game": "full", "1st_half": "1h", "2nd_half": "2h", "1st_quarter": "1q", "2nd_quarter": "2q",
    "3rd_quarter": "3q", "4th_quarter": "4q", "1st_period": "1p", "2nd_period": "2p", "3rd_period": "3p",
    "1st_set": "1s", "2nd_set": "2s", "first_5_innings": "1i5", "1st_5_innings": "1i5",
}
_PERIOD_PREFIX = re.compile(r"^(1st_half|2nd_half|1st_quarter|2nd_quarter|3rd_quarter|4th_quarter|1st_period|2nd_period|3rd_period|1st_set|2nd_set|first_5_innings|1st_5_innings)_")


class SharpApiProvider(BaseProvider):
    name = "sharpapi"

    def __init__(self, api_key: str, budget: Budget, leagues: List[str], min_interval: float = 5.5,
                 max_pages: int = 6):
        self.key = api_key
        self.budget = budget
        self.leagues = leagues
        self.max_pages = max_pages
        self.http = Http("https://api.sharpapi.io/api/v1", headers={"X-API-Key": api_key},
                         rate=RateLimiter(min_interval))

    def enabled(self) -> bool:
        return bool(self.key)

    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.last_fetch_complete = True
        self.fetch_counts = {}
        rows: List[Dict] = []
        targets = self.leagues or [None]
        for league in targets:
            cursor = None
            pages = 0
            while pages < self.max_pages:
                params: Dict = {"is_live": "false", "limit": 200}
                if league:
                    params["league"] = league
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self.http.get("/odds", params)
                except ProviderError as e:
                    self.last_fetch_complete = False
                    log.warning("SharpAPI fetch failed (%s): %s", league or "all", e)
                    break
                self.budget.spend(self.name, 1)
                pages += 1
                rows.extend(data.get("data", []))
                pag = data.get("pagination") or {}
                self.fetch_counts[str(league or "all")] = {"pages": pages, "capped": bool(pag.get("has_more"))}
                if not pag.get("has_more") or not pag.get("next_cursor"):
                    break
                cursor = pag["next_cursor"]
        events = self._rows_to_events(rows, start, end)
        log.info("SharpAPI: %d rows -> %d events in window", len(rows), len(events))
        return events

    # ------------------------------------------------------------------ parse
    def _rows_to_events(self, rows: List[Dict], start: datetime, end: datetime) -> List[Event]:
        counts = Counter()
        by_event: Dict[str, List[Dict]] = defaultdict(list)
        for r in rows:
            if r.get("is_active") is False or r.get("is_live"):
                continue
            by_event[str(r.get("event_id"))].append(r)
        events: List[Event] = []
        for eid, ers in by_event.items():
            r0 = ers[0]
            st = parse_iso(r0.get("event_start_time", ""))
            sport = SPORT_MAP.get(str(r0.get("sport", "")).lower(), "other")
            counts[sport + ":raw_events"] += 1
            if not st or not (start <= st <= end):
                counts[sport + ":outside_window"] += 1
                continue
            counts[sport + ":in_window"] += 1
            e = Event(provider=self.name, provider_event_id=eid, sport=sport,
                      league=str(r0.get("league", "")).replace("_", " ").upper(), league_id=str(r0.get("league", "")),
                      home=r0.get("home_team", ""), away=r0.get("away_team", ""), start=st, sources=[self.name])
            # consolidate the same selection across books -> best price + n_books
            merged: Dict[Tuple, MarketLine] = {}
            for r in ers:
                ml = self._row_to_line(r, sport)
                if not ml:
                    continue
                k = ml.key()
                if k in merged:
                    m = merged[k]
                    m.book_prices.append(ml.price)
                    m.n_books = len(m.book_prices)
                    if ml.price > m.price:
                        m.price, m.bookmaker, m.provider_odd_id = ml.price, ml.bookmaker, ml.provider_odd_id
                else:
                    merged[k] = ml
            e.markets = list(merged.values())
            if e.markets and e.home and e.away:
                counts[sport + ":normalized_events"] += 1
                events.append(e)
        self.normalization_counts = dict(counts)
        log.info("SharpAPI normalization: %s", self.normalization_counts)
        return events

    def _row_to_line(self, r: Dict, sport: str) -> Optional[MarketLine]:
        price = parse_decimal(r.get("odds_decimal"))
        if not price:
            return None
        mtype = str(r.get("market_type", "")).lower()
        stype = str(r.get("selection_type", "")).lower()
        line = r.get("line")
        try:
            line = float(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        period = SEGMENT_PERIOD.get(str(r.get("market_segment", "full_game")).lower(), "full")
        m = _PERIOD_PREFIX.match(mtype)
        if m:
            period = SEGMENT_PERIOD.get(m.group(1), period)
            mtype = mtype[m.end():]
        if r.get("is_player_prop") or mtype.startswith("player_"):
            return None  # unsettleable from this provider's free tier
        if r.get("is_main_line") is False and r.get("is_alternate_line") is True:
            pass  # alt lines are fine as candidates; keep

        market: Optional[str] = None
        selection = stype
        stat, entity = "points", "all"

        if mtype in ("moneyline", "moneyline_3-way", "moneyline_3way", "3way_moneyline", "match_winner"):
            if stype not in ("home", "away", "draw"):
                return None
            # Soccer moneylines at US books are 3-way (a draw loses); every other
            # sport's moneyline is 2-way including overtime.
            market = "result3" if (sport == "football" or stype == "draw" or "3" in mtype) else "result2"
        elif mtype == "draw_no_bet":
            market = "dnb"
        elif mtype == "double_chance":
            market = "dc"
            selection = {"home_draw": "home_draw", "away_draw": "away_draw", "home_away": "home_away"}.get(stype)
            if not selection:
                return None
        elif mtype in ("both_teams_to_score", "btts"):
            market = "btts"
        elif "spread" in mtype or "handicap" in mtype or mtype in ("run_line", "puck_line"):
            if stype not in ("home", "away") or line is None:
                return None
            market = "handicap"
            if "corner" in mtype:
                stat = "corners"
            elif "card" in mtype:
                stat = "cards"
            elif "game" in mtype or "set" in mtype:
                stat = "games" if "game" in mtype else "sets"
        elif mtype == "team_total":
            if line is None:
                return None
            market = "total"
            side = r.get("team_side") or stype.split("_", 1)[0]
            ou = stype.split("_", 1)[1] if "_" in stype else stype
            if side not in ("home", "away") or ou not in ("over", "under"):
                return None
            entity, selection = side, ou
        elif "total" in mtype:
            if stype not in ("over", "under") or line is None:
                return None
            market = "total"
            if "corner" in mtype:
                stat = "corners"
            elif "card" in mtype:
                stat = "cards"
            elif "game" in mtype:
                stat = "games"
            elif "set" in mtype:
                stat = "sets"
            elif "round" in mtype:
                stat = "rounds"
        elif mtype in ("match_result_total_goals", "result_total"):
            if line is None or "_" not in stype:
                return None
            market = "result_total"
        elif mtype in ("match_result_both_teams_to_score", "result_btts"):
            if "_" not in stype:
                return None
            market = "result_btts"
        elif mtype == "correct_score":
            if not re.match(r"^\d+_\d+$", stype):
                return None
            market, selection = "correct_score", stype.replace("_", "-")
        elif mtype in ("halftime_fulltime", "half_time_full_time", "ht_ft"):
            parts = stype.split("_")
            if len(parts) != 2 or not all(p in ("home", "draw", "away") for p in parts):
                return None
            market, selection = "ht_ft", "/".join(parts)
        elif "odd_even" in mtype or mtype.endswith("_parity"):
            if stype not in ("odd", "even"):
                return None
            market = "odd_even"
        elif mtype in ("clean_sheet", "win_to_nil", "team_to_score"):
            side = r.get("team_side")
            if side not in ("home", "away") or stype not in ("yes", "no"):
                return None
            market, entity = mtype, side
        elif mtype == "winning_margin":
            mm = re.match(r"^(home|away)_(\d+)(?:_(\d+|plus))?$", stype)
            if not mm:
                return None
            band = mm.group(2) + ("+" if mm.group(3) == "plus" else f"-{mm.group(3)}" if mm.group(3) else "")
            market, selection = "winning_margin", f"{mm.group(1)}_{band}"
        else:
            return None

        return MarketLine(market=market, selection=selection, price=price, period=period, stat=stat,
                          entity=entity, line=line, bookmaker=str(r.get("sportsbook", "")), n_books=1,
                          provider_odd_id=str(r.get("id", "")), market_name=mtype.replace("_", " ").title(),
                          book_prices=[price])
