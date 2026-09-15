"""SportsGameOdds v2 (https://sportsgameodds.com/docs).

One /v2/events call returns fixtures + every market (consensus fair/book odds,
per-book prices) + results/settled stat values, so it doubles as our most
complete results source. Billing is per *event object*, tracked in Budget.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.odds import american_to_decimal
from predbot.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

SPORT_MAP = {
    "SOCCER": "football", "BASKETBALL": "basketball", "FOOTBALL": "american_football",
    "HOCKEY": "hockey", "BASEBALL": "baseball", "TENNIS": "tennis", "MMA": "mma",
    "HANDBALL": "handball", "GOLF": "golf",
}
SPORT_TO_SGO = {v: k for k, v in SPORT_MAP.items()}

PERIOD_MAP = {"game": "full", "reg": "reg", "1h": "1h", "2h": "2h", "1q": "1q", "2q": "2q", "3q": "3q",
              "4q": "4q", "1p": "1p", "2p": "2p", "3p": "3p", "1s": "1s", "2s": "2s", "3s": "3s",
              "1i5": "1i5"}
STAT_MAP = {"points": "points", "cornerKicks": "corners", "weightedCards": "cards", "yellowCards": "cards",
            "bothTeamsScored": "btts"}


def _f(v) -> Optional[float]:
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return None


class SportsGameOddsProvider(BaseProvider):
    name = "sportsgameodds"

    def __init__(self, api_key: str, budget: Budget, sports: List[str], leagues: List[str],
                 event_limit: int = 25, monthly_limit: int = 1000):
        self.key = api_key
        self.budget = budget
        self.sports = sports
        self.leagues = leagues
        self.event_limit = event_limit
        self.monthly_limit = monthly_limit
        self.http = Http("https://api.sportsgameodds.com/v2", headers={"X-Api-Key": api_key},
                         rate=RateLimiter(1.0))

    def enabled(self) -> bool:
        return bool(self.key)

    # ------------------------------------------------------------------ fetch
    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        allowance = self.budget.monthly_daily_allowance(self.name, self.monthly_limit)
        limit = max(0, min(self.event_limit, allowance))
        if limit <= 0:
            log.warning("SGO: no object allowance left today (monthly limit %d)", self.monthly_limit)
            return []
        if self.budget.is_exhausted(self.name):
            return []                              # tier/quota problem already seen today
        if not self.leagues:
            # the free tier requires a leagueID (or eventID) - sportID-only queries 400
            log.warning("SGO: this subscription tier requires a leagueID - set SGO_LEAGUES "
                        "(comma-separated sportsgameodds.com league IDs) to enable this provider")
            self.budget.mark_exhausted(self.name, "free tier requires SGO_LEAGUES")
            return []
        params: Dict = {
            "startsAfter": iso(start), "startsBefore": iso(end), "oddsAvailable": "true",
            "type": "match", "includeAltLines": "false", "limit": min(limit, 100),
            "leagueID": ",".join(self.leagues),
        }
        try:
            data = self.http.get("/events", params)
        except ProviderError as e:
            if "leagueID" in str(e):
                # tier changed or league id invalid - don't hammer it all day
                self.budget.mark_exhausted(self.name, str(e))
            raise
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, len(events))
        out: List[Event] = []
        for ev in events:
            try:
                e = self._parse_event(ev)
                if e and e.markets:
                    out.append(e)
            except Exception as ex:  # noqa: BLE001
                log.debug("SGO parse error %s: %s", ev.get("eventID"), ex)
        log.info("SGO: %d events with markets (spent %d objects, %d used this month)",
                 len(out), len(events), self.budget.used_month(self.name))
        return out

    # ------------------------------------------------------------------ parse
    def _parse_event(self, ev: Dict) -> Optional[Event]:
        teams = ev.get("teams") or {}
        home = (teams.get("home") or {}).get("names", {}).get("long") or (teams.get("home") or {}).get("names", {}).get("medium")
        away = (teams.get("away") or {}).get("names", {}).get("long") or (teams.get("away") or {}).get("names", {}).get("medium")
        start = parse_iso((ev.get("status") or {}).get("startsAt", ""))
        if not (home and away and start):
            return None
        sport = SPORT_MAP.get(ev.get("sportID", ""), "other")
        players = ev.get("players") or {}
        e = Event(provider=self.name, provider_event_id=ev["eventID"], sport=sport,
                  league=str(ev.get("leagueID", "")).replace("_", " "), league_id=ev.get("leagueID", ""),
                  home=home, away=away, start=start, sources=[self.name])
        for odd_id, o in (ev.get("odds") or {}).items():
            ml = self._parse_odd(odd_id, o, sport, players)
            if ml:
                e.markets.append(ml)
        return e

    def _parse_odd(self, odd_id: str, o: Dict, sport: str, players: Dict) -> Optional[MarketLine]:
        if o.get("cancelled") or o.get("betTypeID") == "prop":
            return None
        if not o.get("bookOddsAvailable", True) and not o.get("bookOdds"):
            return None
        stat_id = o.get("statID", "")
        entity_id = o.get("statEntityID", "")
        period = PERIOD_MAP.get(o.get("periodID", "game"), o.get("periodID", "game"))
        if sport == "football" and period == "reg":
            period = "full"
        bet, side = o.get("betTypeID"), o.get("sideID")
        price = american_to_decimal(o.get("bookOdds"))
        by_book = o.get("byBookmaker") or {}
        book_prices = [american_to_decimal(b.get("odds")) for b in by_book.values() if b.get("available")]
        book_prices = [p for p in book_prices if p]
        if not price and book_prices:
            price = max(book_prices)
        if not price:
            return None
        n_books = max(1, len(book_prices))
        fair = american_to_decimal(o.get("fairOdds"))
        is_player = entity_id not in ("home", "away", "all")
        entity = entity_id
        if is_player:
            entity = (players.get(entity_id) or {}).get("name") or entity_id
        stat = STAT_MAP.get(stat_id, stat_id)

        market_name = o.get("marketName", "")
        line: Optional[float] = None
        market: str
        selection: str

        if bet == "ml3way" and stat == "points" and not is_player:
            if side in ("home", "away", "draw"):
                market, selection = "result3", side
            elif side == "home+draw":
                market, selection = "dc", "home_draw"
            elif side == "away+draw":
                market, selection = "dc", "away_draw"
            elif side == "not_draw":
                market, selection = "dc", "home_away"
            else:
                return None
        elif bet == "ml" and stat == "points" and not is_player:
            # 2-way moneyline: a level score pushes. In football that is
            # exactly "draw no bet"; elsewhere it is the regular moneyline.
            market = "dnb" if sport == "football" else "result2"
            selection = side
            if selection not in ("home", "away"):
                return None
        elif bet == "sp" and not is_player:
            line = _f(o.get("bookSpread") if o.get("bookSpread") is not None else o.get("fairSpread"))
            if line is None or side not in ("home", "away"):
                return None
            if _f(o.get("fairSpread")) != line:
                fair = None
            market, selection = "handicap", side
        elif bet == "ou":
            line = _f(o.get("bookOverUnder") if o.get("bookOverUnder") is not None else o.get("fairOverUnder"))
            if line is None or side not in ("over", "under"):
                return None
            if _f(o.get("fairOverUnder")) != line:
                fair = None
            if is_player:
                market, selection = "prop", side
            else:
                market, selection = "total", side
        elif bet == "yn":
            if stat == "btts" and not is_player:
                market, selection, stat = "btts", side, "points"
            else:
                market, selection = "prop", side
        elif bet == "eo":
            market, selection = ("prop", side) if is_player else ("odd_even", side)
        else:
            return None

        ml = MarketLine(market=market, selection=selection, price=price, period=period, stat=stat,
                        entity=entity if market != "btts" else "all", line=line, fair_price=fair,
                        bookmaker="consensus", n_books=n_books, provider_odd_id=odd_id,
                        label=market_name, market_name=market_name, is_player_prop=is_player,
                        book_prices=book_prices)
        if market == "prop" and not o.get("scoringSupported", False):
            return None   # cannot be settled -> never a candidate
        return ml

    # ---------------------------------------------------------------- results
    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        if not self.budget.can_spend(self.name, 1, monthly_limit=self.monthly_limit):
            return None
        data = self.http.get("/events", {"eventID": provider_event_id, "expandResults": "true"})
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, max(1, len(events)))
        if not events:
            return None
        return self._parse_result(events[0])

    def find_result(self, sport: str, home: str, away: str, start: datetime) -> Optional[MatchResult]:
        sgo_sport = SPORT_TO_SGO.get(sport)
        if not sgo_sport:
            return None
        if not self.budget.can_spend(self.name, 10, monthly_limit=self.monthly_limit):
            return None
        params = {"sportID": sgo_sport, "startsAfter": iso(start - timedelta(hours=2)),
                  "startsBefore": iso(start + timedelta(hours=2)), "type": "match",
                  "expandResults": "true", "limit": 50}
        try:
            data = self.http.get("/events", params)
        except ProviderError as e:
            log.warning("SGO find_result failed: %s", e)
            return None
        events = data.get("data", []) if isinstance(data, dict) else []
        self.budget.spend(self.name, len(events))
        for ev in events:
            teams = ev.get("teams") or {}
            h = (teams.get("home") or {}).get("names", {}).get("long", "")
            a = (teams.get("away") or {}).get("names", {}).get("long", "")
            if same_team(h, home) and same_team(a, away):
                return self._parse_result(ev)
        return None

    @staticmethod
    def _parse_result(ev: Dict) -> MatchResult:
        st = ev.get("status") or {}
        if st.get("cancelled"):
            status = "cancelled"
        elif st.get("completed") or st.get("finalized") or st.get("ended"):
            status = "final"
        elif st.get("started"):
            status = "in_progress"
        else:
            status = "unknown"
        res = MatchResult(status=status, source="sportsgameodds")
        sport = SPORT_MAP.get(ev.get("sportID", ""), "other")

        # 1) results object: {periodID: {home: {statID: v}, away: {...}, all: {...}}}
        results = ev.get("results") or {}
        for period_id, per in results.items():
            if not isinstance(per, dict):
                continue
            p = PERIOD_MAP.get(period_id, period_id)
            if sport == "football" and p == "reg":
                p = "full"
            hs, as_ = per.get("home") or {}, per.get("away") or {}
            for stat_id, stat in STAT_MAP.items():
                if stat == "btts":
                    continue
                hv, av = _f(hs.get(stat_id)), _f(as_.get(stat_id))
                if hv is not None and av is not None:
                    if stat == "points":
                        res.scores.setdefault(p, (hv, av))
                    else:
                        res.stats.setdefault((stat, p), (hv, av))

        # 2) per-odd settled values (authoritative for the exact market)
        odds = ev.get("odds") or {}
        per_stat: Dict = {}
        for odd_id, o in odds.items():
            sc = _f(o.get("score"))
            if sc is None:
                continue
            res.odd_scores[odd_id] = sc
            p = PERIOD_MAP.get(o.get("periodID", ""), o.get("periodID", ""))
            if sport == "football" and p == "reg":
                p = "full"
            per_stat.setdefault((o.get("statID"), p), {})[o.get("statEntityID")] = sc
        for (stat_id, p), sides in per_stat.items():
            if "home" in sides and "away" in sides:
                stat = STAT_MAP.get(stat_id, stat_id)
                if stat == "points":
                    res.scores.setdefault(p, (sides["home"], sides["away"]))
                elif stat != "btts":
                    res.stats.setdefault((stat, p), (sides["home"], sides["away"]))
        if "full" not in res.scores and "reg" in res.scores:
            res.scores["full"] = res.scores["reg"]
        if status == "final" and not res.scores:
            res.status = "unknown"
        return res
