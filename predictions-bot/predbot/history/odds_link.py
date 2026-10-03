"""Historical bookmaker odds from football-data.co.uk CSVs, LINKED to results already in the store.

Evaluation data only. Odds are reference prices a model is compared against; no model in this repository reads them
while fitting or predicting. This module therefore never inserts a match and never creates a participant: it
attaches raw odds to an existing ``match_results`` row or reports why it could not.

Linking is deliberately conservative:
* a CSV team name resolves through the identity registry (stored mapping, alias, exact name, unambiguous fuzzy) and must
  be a club that already plays in that competition-season;
* a name that still does not resolve is learned ONLY from fixtures: across the file, the stored match with the same
  competition, a kickoff date within one day and the same final score must name one club in >= 80% of at least 3
  appearances, with the club not already taken by another name. Each learned alias is reported;
* a fixture links only when exactly ONE stored match has the same two clubs within one day AND the same score.
  Zero matches, several matches or a score disagreement are reported and never guessed.

Quote kinds follow football-data.co.uk: ``pre`` = the columns collected before kick-off (``PSH``, ``B365H``, ``AvgH``,
...), ``close`` = the closing columns (``PSCH``, ``B365CH``, ``AvgCH``, ...). Raw odds are stored exactly as published;
probabilities are derived later and elsewhere.
"""
from __future__ import annotations

import csv
import io
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from predbot.history.ingest import SPORT
from predbot.history.store import HistoryStore
from predbot.storage.identity import IdentityRegistry
from predbot.util.names import canonical_team
from predbot.util.timeutil import utcnow

SOURCE = "football_data_co_uk"
BASE_URL = "https://www.football-data.co.uk/mmz4281"
DIVISIONS = {"E0": "epl", "E1": "championship", "SP1": "la_liga", "I1": "serie_a", "D1": "bundesliga", "F1": "ligue_1",
             "N1": "eredivisie", "P1": "primeira"}
SEASON_DIR = {"2024": "2425", "2025": "2526", "2026": "2627"}
COURTESY_SECONDS = 2.0

ONE_X_TWO = {"pinnacle": {"pre": ("PSH", "PSD", "PSA"), "close": ("PSCH", "PSCD", "PSCA")},
             "bet365": {"pre": ("B365H", "B365D", "B365A"), "close": ("B365CH", "B365CD", "B365CA")},
             "market_avg": {"pre": ("AvgH", "AvgD", "AvgA"), "close": ("AvgCH", "AvgCD", "AvgCA")}}
OVER_UNDER_25 = {"pinnacle": {"pre": ("P>2.5", "P<2.5"), "close": ("PC>2.5", "PC<2.5")},
                 "bet365": {"pre": ("B365>2.5", "B365<2.5"), "close": ("B365C>2.5", "B365C<2.5")},
                 "market_avg": {"pre": ("Avg>2.5", "Avg<2.5"), "close": ("AvgC>2.5", "AvgC<2.5")}}
OTHER_FAMILIES = {"asian_handicap": ("AH",), "btts": ("BTTS", "BTS"), "max_odds": ("Max",)}


def url_for(div: str, season: str) -> str:
    return f"{BASE_URL}/{SEASON_DIR[season]}/{div}.csv"


def plan_csv(db, divisions: Iterable[str], seasons: Iterable[str], now: Optional[datetime] = None) -> List[Tuple[str, str, str]]:
    """(division, season, url) of the files that would be downloaded. The ledger makes each past file once-only."""
    store, now = HistoryStore(db), now or utcnow()
    return [(d, s, url_for(d, s)) for d in divisions for s in seasons
            if store.needs_fetch(SOURCE, DIVISIONS[d], s, now)]


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; historical odds import)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def download_csv(db, plan, cache_dir: Path, get: Callable[[str], str] = _http_get,
                 sleep: Callable[[float], None] = time.sleep, now: Optional[datetime] = None) -> Dict[str, dict]:
    """Download each planned file once into ``cache_dir`` and record it in the ledger. Failures are recorded and
    backed off, never retried in a loop."""
    store, now = HistoryStore(db), now or utcnow()
    report: Dict[str, dict] = {}
    for i, (div, season, url) in enumerate(plan):
        if i:
            sleep(COURTESY_SECONDS)
        target = cache_dir / season / f"{div}.csv"
        try:
            text = get(url)
            if "HomeTeam" not in text.splitlines()[0]:
                raise ValueError("not a football-data.co.uk results file")
        except Exception as exc:                                               # noqa: BLE001 - recorded, retried later
            store.mark_fetch(SOURCE, DIVISIONS[div], season, "failed", 0, now)
            report[f"{div}:{season}"] = {"url": url, "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        rows = max(0, text.count("\n") - 1)
        store.mark_fetch(SOURCE, DIVISIONS[div], season, "complete", rows, now)
        report[f"{div}:{season}"] = {"url": url, "bytes": len(text), "rows": rows}
    return report


# ------------------------------------------------------------------------------------------------ parsing
def _num(row: dict, col: str) -> Optional[float]:
    try:
        v = float(str(row.get(col, "")).strip())
    except ValueError:
        return None
    return v if v > 1.0 else None


def _csv_date(row: dict) -> Optional[date]:
    raw = (row.get("Date") or "").strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def column_audit(fieldnames: Iterable[str], rows: List[dict]) -> dict:
    """Which of the odds columns exist and how many rows carry a usable price in each (every column of every set)."""
    names = list(fieldnames)
    out: dict = {"1x2": {}, "ou25": {}, "other_columns": {}}
    for market, spec in (("1x2", ONE_X_TWO), ("ou25", OVER_UNDER_25)):
        for book, kinds in spec.items():
            for kind, cols in kinds.items():
                out[market][f"{book}:{kind}"] = {"columns": list(cols), "present": all(c in names for c in cols),
                                                 "rows": sum(1 for r in rows if all(_num(r, c) for c in cols))}
    for family, prefixes in OTHER_FAMILIES.items():
        hit = [n for n in names if any(n.startswith(p) for p in prefixes)]
        if hit:
            out["other_columns"][family] = hit
    return out


def _quotes(row: dict) -> List[Tuple[str, str, str, tuple, str]]:
    out = []
    for market, spec in (("1x2", ONE_X_TWO), ("ou25", OVER_UNDER_25)):
        for book, kinds in spec.items():
            for kind, cols in kinds.items():
                vals = [_num(row, c) for c in cols]
                if all(vals):
                    out.append((book, market, kind, tuple(vals), ",".join(cols)))
    return out


# ------------------------------------------------------------------------------------------------ linking
def _stored(db, comp: str, season: str) -> List[dict]:
    rows = db.query("SELECT id, kickoff_utc, home_id, away_id, home_goals, away_goals FROM match_results "
                    "WHERE competition_id=? AND season=? AND sport=?", (comp, season, SPORT))
    return [dict(r, day=datetime.fromisoformat(r["kickoff_utc"].replace("Z", "+00:00")).date()) for r in rows]


def link_csv(db, text: str, comp: str, season: str) -> dict:
    """Attach the raw odds of one CSV to existing matches. Never inserts a match or a participant."""
    store, registry = HistoryStore(db), IdentityRegistry(db)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = [r for r in reader if (r.get("HomeTeam") or "").strip()]
    stored = _stored(db, comp, season)
    by_day: Dict[date, List[dict]] = defaultdict(list)
    for m in stored:
        by_day[m["day"]].append(m)
    known = {m["home_id"] for m in stored} | {m["away_id"] for m in stored}

    def near(d: date) -> List[dict]:
        return [m for k in (-1, 0, 1) for m in by_day.get(d + timedelta(days=k), [])]

    played = []
    for r in rows:
        try:
            hg, ag = int(r["FTHG"]), int(r["FTAG"])
        except (KeyError, TypeError, ValueError):
            continue
        d = _csv_date(r)
        if d is not None:
            played.append((r, d, hg, ag))

    names = {(r["HomeTeam"].strip()) for r, *_ in played} | {(r["AwayTeam"].strip()) for r, *_ in played}
    resolved: Dict[str, str] = {}
    ambiguous_names: List[str] = []
    for name in sorted(names):
        try:
            cid = registry.find_participant(SOURCE, SPORT, name)
        except ValueError:
            ambiguous_names.append(name)
            continue
        if cid in known:
            resolved[name] = cid

    learned: Dict[str, str] = {}
    for _ in range(3):                                           # fixture-anchored learning, repeated as opponents resolve
        progress = False
        votes: Dict[str, Counter] = defaultdict(Counter)
        for r, d, hg, ag in played:
            h, a = r["HomeTeam"].strip(), r["AwayTeam"].strip()
            for side, name, other in (("home_id", h, a), ("away_id", a, h)):
                if name in resolved:
                    continue
                opp = resolved.get(other)
                opp_side = "away_id" if side == "home_id" else "home_id"
                for m in near(d):
                    if (m["home_goals"], m["away_goals"]) != (hg, ag):
                        continue
                    if opp is not None and m[opp_side] != opp:
                        continue
                    votes[name][m[side]] += 1
        taken = set(resolved.values())
        for name, tally in sorted(votes.items()):
            (cid, top), total = tally.most_common(1)[0], sum(tally.values())
            if top >= 3 and top / total >= 0.8 and cid not in taken and registry.add_alias(SPORT, name, cid):
                resolved[name] = learned[name] = cid
                taken.add(cid)
                progress = True
        if not progress:
            break

    counts: Counter = Counter()
    unresolved: Counter = Counter()
    linked_ids: set = set()
    odds_written = 0
    for r, d, hg, ag in played:
        counts["csv_matches"] += 1
        h, a = r["HomeTeam"].strip(), r["AwayTeam"].strip()
        if h not in resolved or a not in resolved:
            counts["unresolved_team"] += 1
            for n in (h, a):
                if n not in resolved:
                    unresolved[n] += 1
            continue
        cands = [m for m in near(d) if m["home_id"] == resolved[h] and m["away_id"] == resolved[a]]
        if not cands:
            counts["unmatched"] += 1
            continue
        if len(cands) > 1:
            counts["ambiguous"] += 1
            continue
        m = cands[0]
        if (m["home_goals"], m["away_goals"]) != (hg, ag):
            counts["score_mismatch"] += 1
            continue
        if m["id"] in linked_ids:
            counts["duplicate_csv_row"] += 1
            continue
        linked_ids.add(m["id"])
        quotes = _quotes(r)
        counts["linked" if quotes else "linked_without_odds"] += 1
        for book, market, kind, prices, cols in quotes:
            odds_written += store.add_odds_ext(m["id"], book, market, kind, prices, cols)
    counts["odds_rows_written"] = odds_written
    counts["csv_rows_not_played"] = len(rows) - len(played)
    return {"counts": dict(counts), "unresolved_names": dict(unresolved), "ambiguous_names": ambiguous_names,
            "learned_aliases": {k: v for k, v in learned.items()}, "stored_matches": len(stored),
            "columns": column_audit(reader.fieldnames or [], played and [p[0] for p in played] or [])}


def link_cached(db, cache_dir: Path, plan_seasons: Iterable[str], divisions: Iterable[str]) -> Dict[str, dict]:
    report = {}
    with db.transaction():
        for season in plan_seasons:
            for div in divisions:
                path = cache_dir / season / f"{div}.csv"
                if path.exists():
                    report[f"{div}:{season}"] = link_csv(db, path.read_text(encoding="utf-8"), DIVISIONS[div], season)
    return report
