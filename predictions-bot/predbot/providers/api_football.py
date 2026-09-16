"""API-Football v3 (api-sports.io / RapidAPI). Football only.

Requests used per daily run (free plan = 100/day):
  1 x /fixtures?date        per UTC date in the window (1-2)
  N x /odds?date&page       (APIF_ODDS_PAGES, default 3)  -> ~10 fixtures per page, every bookmaker/market
  M x /predictions?fixture  (APIF_PREDICTIONS_PER_RUN)     -> model features for the best fixtures
Settlement: /fixtures?ids=a-b-c (20 per request) and /fixtures/statistics only when a
corners/cards pick needs it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from predbot.providers.base import BaseProvider, Event, MarketLine, MatchResult
from predbot.providers.budget import Budget
from predbot.util.http import Http, ProviderError, RateLimiter
from predbot.util.names import same_team
from predbot.util.odds import parse_decimal
from predbot.util.timeutil import parse_iso

log = logging.getLogger(__name__)

# Leagues we prefer when the free quota forces us to choose (API-Football ids)
PRIORITY_LEAGUES = [2, 3, 848, 39, 140, 135, 78, 61, 88, 94, 144, 203, 253, 71, 128, 262, 307, 40, 179,
                    207, 218, 119, 113, 106, 197, 235, 169, 292, 98, 1, 4, 5, 9, 13, 11, 17, 18, 45, 143,
                    137, 81, 66, 90, 96, 45]
_PRI = {lid: i for i, lid in enumerate(PRIORITY_LEAGUES)}

FINAL_STATUS = {"FT", "AET", "PEN"}
VOID_STATUS = {"PST", "CANC", "ABD", "AWD", "WO", "SUSP", "INT"}

_OU = re.compile(r"^(over|under)\s*([0-9.]+)$", re.I)
_TEAM_LINE = re.compile(r"^(home|away)\s*([+-]?[0-9.]+)$", re.I)
_MARGIN = re.compile(r"^(home|away)\s*(?:by)?\s*(\d+)(\+|\s*-\s*(\d+))?$", re.I)


def _side(v: str) -> Optional[str]:
    v = v.strip().lower()
    return {"home": "home", "draw": "draw", "away": "away", "1": "home", "x": "draw", "2": "away"}.get(v)


class ApiFootballProvider(BaseProvider):
    name = "api_football"

    def __init__(self, api_key: str, budget: Budget, host: str = "v3.football.api-sports.io",
                 league_ids: Optional[List[str]] = None, daily_limit: int = 100, predictions_per_run: int = 25,
                 odds_pages: int = 3, settlement_reserve: int = 15):
        self.key = api_key
        self.budget = budget
        self.host = host
        self.league_ids = [int(x) for x in (league_ids or []) if str(x).isdigit()]
        self.daily_limit = daily_limit
        self.predictions_per_run = predictions_per_run
        self.odds_pages = odds_pages
        self.settlement_reserve = settlement_reserve
        if "rapidapi" in host:
            headers = {"x-rapidapi-key": api_key, "x-rapidapi-host": host}
        else:
            headers = {"x-apisports-key": api_key}
        self.http = Http(f"https://{host}", headers=headers, rate=RateLimiter(0.4))

    def enabled(self) -> bool:
        return bool(self.key)

    # ---------------------------------------------------------------- helpers
    def _call(self, path: str, params: Dict, reserve: int = 0) -> Optional[Dict]:
        if self.budget.is_exhausted(self.name):
            return None                            # daily plan limit hit - stop calling until tomorrow
        if not self.budget.can_spend(self.name, 1, daily_limit=self.daily_limit - reserve):
            return None
        data = self.http.get(path, params)
        errs = data.get("errors") if isinstance(data, dict) else None
        if errs and (isinstance(errs, dict) and errs or isinstance(errs, list) and errs):
            msg = str(errs)
            if "request limit" in msg or "requests" in errs:
                # plan quota exhausted: mark it so every later call today is skipped
                self.budget.mark_exhausted(self.name, msg)
            raise ProviderError(f"API-Football error: {errs}")
        self.budget.spend(self.name, 1)  # only successful calls count against the quota
        return data

    # ------------------------------------------------------------------ fetch
    def fetch_events(self, start: datetime, end: datetime) -> List[Event]:
        self.last_fetch_complete = True
        self.fetch_counts = {"raw_fixtures": 0, "in_window": 0, "league_rejected": 0,
                             "odds_rows": 0, "events_with_markets": 0}
        fixtures: Dict[int, Dict] = {}
        d = start.date()
        while d <= end.date():
            data = self._call("/fixtures", {"date": d.strftime("%Y-%m-%d")}, reserve=self.settlement_reserve)
            if data is None:
                self.last_fetch_complete = False
                break
            for fx in data.get("response", []):
                self.fetch_counts["raw_fixtures"] += 1
                st = parse_iso(fx["fixture"]["date"])
                if not st or not (start <= st <= end):
                    continue
                if fx["fixture"]["status"]["short"] != "NS":
                    continue
                self.fetch_counts["in_window"] += 1
                lid = int(fx["league"]["id"])
                if self.league_ids and lid not in self.league_ids:
                    self.fetch_counts["league_rejected"] += 1
                    continue
                fixtures[int(fx["fixture"]["id"])] = fx
            d += timedelta(days=1)
        if not fixtures:
            return []

        # Odds for the date(s) - paginated, every bookmaker & bet type.
        events: Dict[int, Event] = {}
        for d in sorted({parse_iso(fx["fixture"]["date"]).date() for fx in fixtures.values()}):
            page, total = 1, 1
            while page <= total and page <= self.odds_pages:
                params = {"date": d.strftime("%Y-%m-%d"), "page": page}
                try:
                    data = self._call("/odds", params, reserve=self.settlement_reserve)
                except ProviderError as e:
                    self.last_fetch_complete = False
                    # free plans cap the page parameter - keep whatever we already have
                    log.warning("odds page %d for %s failed: %s", page, d, e)
                    break
                if data is None:
                    self.last_fetch_complete = False
                    break
                total = int((data.get("paging") or {}).get("total", 1))
                for item in data.get("response", []):
                    self.fetch_counts["odds_rows"] += 1
                    fid = int(item["fixture"]["id"])
                    fx = fixtures.get(fid)
                    if not fx:
                        continue
                    ev = events.get(fid) or self._fixture_to_event(fx)
                    self._merge_odds(ev, item.get("bookmakers", []))
                    if ev.markets:
                        events[fid] = ev
                page += 1

        out = list(events.values())
        self.fetch_counts["events_with_markets"] = len(out)
        out.sort(key=lambda e: _PRI.get(int(e.extra.get("league_id", 0)), 999))
        # Predictions for the most relevant fixtures (model features).
        # The free plan allows 10 requests/minute, so pace this phase to stay
        # under the limit and restore the fast rate when done.
        n = 0
        fast = self.http.rate.min_interval if self.http.rate else None
        if self.http.rate:
            self.http.rate.min_interval = max(self.http.rate.min_interval, 7.0)
        try:
            for ev in out:
                if n >= self.predictions_per_run:
                    break
                try:
                    data = self._call("/predictions", {"fixture": ev.provider_event_id}, reserve=self.settlement_reserve)
                except ProviderError as e:
                    log.warning("predictions failed for %s: %s", ev.provider_event_id, e)
                    continue
                if data is None:
                    break
                resp = data.get("response") or []
                if resp:
                    ev.extra["prediction"] = resp[0]
                n += 1
        finally:
            if self.http.rate and fast is not None:
                self.http.rate.min_interval = fast
        log.info("API-Football: %d fixtures with odds, %d predictions (used %d/%d today)",
                 len(out), n, self.budget.used_today(self.name), self.daily_limit)
        return out

    def _fixture_to_event(self, fx: Dict) -> Event:
        ev = Event(provider=self.name, provider_event_id=str(fx["fixture"]["id"]), sport="football",
                   league=fx["league"]["name"], league_id=str(fx["league"]["id"]),
                   home=fx["teams"]["home"]["name"], away=fx["teams"]["away"]["name"],
                   start=parse_iso(fx["fixture"]["date"]), sources=[self.name])
        ev.extra["league_id"] = fx["league"]["id"]
        ev.extra["country"] = fx["league"].get("country")
        ev.extra["home_id"], ev.extra["away_id"] = fx["teams"]["home"]["id"], fx["teams"]["away"]["id"]
        return ev

    # ------------------------------------------------------------ odds parse
    def _merge_odds(self, ev: Event, bookmakers: List[Dict]) -> None:
        merged: Dict[Tuple, MarketLine] = {ml.key(): ml for ml in ev.markets}
        for bk in bookmakers:
            bname = bk.get("name", "")
            for bet in bk.get("bets", []):
                for val in bet.get("values", []):
                    ml = self._parse_value(bet.get("name", ""), int(bet.get("id", 0)), str(val.get("value", "")), val.get("odd"))
                    if not ml:
                        continue
                    ml.bookmaker = bname
                    k = ml.key()
                    if k in merged:
                        m = merged[k]
                        m.book_prices.append(ml.price)
                        m.n_books = len(m.book_prices)
                        if ml.price > m.price:
                            m.price, m.bookmaker = ml.price, bname
                    else:
                        merged[k] = ml
        ev.markets = list(merged.values())

    def _parse_value(self, bet_name: str, bet_id: int, value: str, odd) -> Optional[MarketLine]:
        price = parse_decimal(odd)
        if not price:
            return None
        name = bet_name.strip().lower()
        v = value.strip()
        period, stat, entity = "full", "points", "all"
        if "first half" in name or "1st half" in name or "(1st half)" in name:
            period = "1h"
        elif "second half" in name or "2nd half" in name:
            period = "2h"
        if "corner" in name:
            stat = "corners"
        elif "card" in name:
            stat = "cards"

        def mk(market, selection, line=None, ent="all"):
            return MarketLine(market=market, selection=selection, price=price, period=period, stat=stat,
                              entity=ent, line=line, n_books=1, provider_odd_id=f"{bet_id}:{value}",
                              market_name=bet_name, book_prices=[price])

        # --- result markets ------------------------------------------------
        if name in ("match winner", "1x2", "first half winner", "second half winner", "corners 1x2", "cards 1x2"):
            s = _side(v)
            return mk("result3", s) if s else None
        if name in ("home/away", "draw no bet", "draw no bet (1st half)", "draw no bet (2nd half)"):
            s = _side(v)
            return mk("dnb", s) if s in ("home", "away") else None
        if name.startswith("double chance"):
            key = {"home/draw": "home_draw", "draw/away": "away_draw", "home/away": "home_away",
                   "1x": "home_draw", "x2": "away_draw", "12": "home_away"}.get(v.lower().replace(" ", ""))
            return mk("dc", key) if key else None
        # --- totals ---------------------------------------------------------
        m = _OU.match(v)
        if m and ("over/under" in name or "over under" in name or name.startswith("total")):
            ent = "home" if ("home" in name or name.endswith("- home")) else "away" if ("away" in name or name.endswith("- away")) else "all"
            return mk("total", m.group(1).lower(), float(m.group(2)), ent)
        if name.startswith("goals over/under") or name in ("total goals",):
            if m:
                return mk("total", m.group(1).lower(), float(m.group(2)))
            return None
        # --- handicaps ------------------------------------------------------
        if "asian handicap" in name or name in ("handicap", "corners asian handicap", "cards asian handicap"):
            t = _TEAM_LINE.match(v)
            if not t:
                return None
            return mk("handicap", t.group(1).lower(), float(t.group(2)))
        # --- BTTS -----------------------------------------------------------
        if name.startswith("both teams score") or name.startswith("both teams to score"):
            s = v.lower()
            return mk("btts", s) if s in ("yes", "no") else None
        # --- odd/even -------------------------------------------------------
        if "odd/even" in name or "odd / even" in name:
            s = v.lower()
            ent = "home" if name.startswith("home") else "away" if name.startswith("away") else "all"
            return mk("odd_even", s, ent=ent) if s in ("odd", "even") else None
        # --- combos ---------------------------------------------------------
        if name in ("ht/ft double", "halftime/fulltime", "ht/ft"):
            parts = [_side(p) for p in v.split("/")]
            if len(parts) == 2 and all(parts):
                return mk("ht_ft", f"{parts[0]}/{parts[1]}")
            return None
        if name in ("exact score", "correct score", "correct score - first half"):
            mm = re.match(r"^(\d+)[:-](\d+)$", v)
            return mk("correct_score", f"{mm.group(1)}-{mm.group(2)}") if mm else None
        if name in ("results/both teams score", "result/both teams score", "result & btts"):
            parts = v.split("/")
            if len(parts) == 2 and _side(parts[0]) and parts[1].strip().lower() in ("yes", "no"):
                return mk("result_btts", f"{_side(parts[0])}_{parts[1].strip().lower()}")
            return None
        if name in ("result/total goals", "result & over/under", "match result and over/under"):
            parts = v.split("/")
            if len(parts) == 2 and _side(parts[0]):
                mm = _OU.match(parts[1].strip())
                if mm:
                    return mk("result_total", f"{_side(parts[0])}_{mm.group(1).lower()}", float(mm.group(2)))
            return None
        if name.startswith("clean sheet"):
            ent = "home" if "home" in name else "away" if "away" in name else None
            s = v.lower()
            return mk("clean_sheet", s, ent=ent) if ent and s in ("yes", "no") else None
        if name.startswith("win to nil"):
            ent = "home" if "home" in name else "away" if "away" in name else None
            s = v.lower()
            return mk("win_to_nil", s, ent=ent) if ent and s in ("yes", "no") else None
        if name in ("home team score a goal", "away team score a goal", "home team to score", "away team to score"):
            ent = "home" if name.startswith("home") else "away"
            s = v.lower()
            return mk("team_to_score", s, ent=ent) if s in ("yes", "no") else None
        if name == "winning margin":
            if v.lower() == "draw":
                return mk("winning_margin", "draw_0")
            mm = _MARGIN.match(v)
            if not mm:
                return None
            band = mm.group(2) + ("+" if mm.group(3) == "+" else f"-{mm.group(4)}" if mm.group(4) else "")
            return mk("winning_margin", f"{mm.group(1).lower()}_{band}")
        return None

    # ---------------------------------------------------------------- results
    def fetch_results_batch(self, fixture_ids: List[str]) -> Dict[str, MatchResult]:
        out: Dict[str, MatchResult] = {}
        ids = [str(i) for i in fixture_ids]
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            data = self._call("/fixtures", {"ids": "-".join(chunk)})
            if data is None:
                break
            for fx in data.get("response", []):
                out[str(fx["fixture"]["id"])] = self._parse_fixture_result(fx)
        return out

    def fetch_result(self, provider_event_id: str, event_hint: Optional[Dict] = None) -> Optional[MatchResult]:
        return self.fetch_results_batch([provider_event_id]).get(provider_event_id)

    def find_result(self, sport: str, home: str, away: str, start: datetime) -> Optional[MatchResult]:
        if sport != "football":
            return None
        data = self._call("/fixtures", {"date": start.strftime("%Y-%m-%d")})
        if data is None:
            return None
        for fx in data.get("response", []):
            if same_team(fx["teams"]["home"]["name"], home) and same_team(fx["teams"]["away"]["name"], away):
                return self._parse_fixture_result(fx)
        return None

    def fetch_statistics(self, fixture_id: str) -> Dict[Tuple[str, str], Tuple[float, float]]:
        """Corners / cards for a finished fixture (1 request)."""
        data = self._call("/fixtures/statistics", {"fixture": fixture_id})
        if data is None:
            return {}
        sides: Dict[str, Dict[str, float]] = {}
        for i, team_block in enumerate(data.get("response", [])[:2]):
            vals = {s["type"]: (s.get("value") or 0) for s in team_block.get("statistics", [])}
            side = "home" if i == 0 else "away"
            corners = float(vals.get("Corner Kicks") or 0)
            cards = float(vals.get("Yellow Cards") or 0) + float(vals.get("Red Cards") or 0)
            sides[side] = {"corners": corners, "cards": cards}
        if len(sides) < 2:
            return {}
        return {("corners", "full"): (sides["home"]["corners"], sides["away"]["corners"]),
                ("cards", "full"): (sides["home"]["cards"], sides["away"]["cards"])}

    @staticmethod
    def _parse_fixture_result(fx: Dict) -> MatchResult:
        short = fx["fixture"]["status"]["short"]
        if short in FINAL_STATUS:
            status = "final"
        elif short in VOID_STATUS:
            status = "postponed" if short == "PST" else "cancelled"
        elif short == "NS":
            status = "unknown"
        else:
            status = "in_progress"
        res = MatchResult(status=status, source="api_football")
        sc = fx.get("score") or {}
        ht, ft = sc.get("halftime") or {}, sc.get("fulltime") or {}
        if ft.get("home") is not None and ft.get("away") is not None:
            res.scores["full"] = (float(ft["home"]), float(ft["away"]))
            res.scores["reg"] = res.scores["full"]
        elif status == "final" and fx.get("goals", {}).get("home") is not None:
            res.scores["full"] = (float(fx["goals"]["home"]), float(fx["goals"]["away"]))
        if ht.get("home") is not None and ht.get("away") is not None:
            res.scores["1h"] = (float(ht["home"]), float(ht["away"]))
            if "full" in res.scores:
                f, h = res.scores["full"], res.scores["1h"]
                res.scores["2h"] = (f[0] - h[0], f[1] - h[1])
        if status == "final" and "full" not in res.scores:
            res.status = "unknown"
        return res
