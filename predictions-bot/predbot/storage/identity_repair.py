"""Find (and, only when asked, remove) identity rows written by the old, looser identity rules.

Before the prediction-integrity fix, US-style names were reduced to their last word ("Kentucky Wildcats" and
"Arizona Wildcats" -> "wildcats", "Maccabi Tel Aviv" and "Hapoel Tel Aviv" -> "aviv") and an unknown team was
learned as an alias of whichever club a known opponent had played near that time, with no name evidence. That
could attach one provider's game to a different canonical game. The resolver now ignores such rows when it reads
them; this module reports them and can delete the LOOKUP rows so they stop occupying the tables.

What it may delete (``apply=True`` only, after a verified backup):

* ``provider_identity`` participant mappings whose name key no longer describes the participant they point to;
* ``participant_aliases`` rows with no name evidence for their participant;
* ``provider_identity`` event mappings attached to a canonical event whose stored predictions name a different team
  (a false merge), so those provider events are resolved afresh.

What it never deletes: canonical participants or events, predictions, memberships, candidates, results, ratings or
any history. Those are REPORTED so an operator can decide - the published record is not rewritten silently.

The report also lists the OPAQUE canonical ids (hashes, no names) of the affected participants and events, and
separates ratings that are deterministically contaminated (a stored pick attributed a side whose name is not that
participant to its id - the settlement Elo update then rated the wrong team) from ratings that are only possibly
touched (a participant whose lookup rows were wrong). Logs carry counts only; no team names or provider ids ever.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import defaultdict
from typing import Dict, List, Set

from predbot.storage.identity import CURATED_ALIAS_SOURCES
from predbot.util.names import canonical_team, related_names, same_participant

log = logging.getLogger(__name__)
#: how many opaque ids the report lists per category (the counts are always complete)
ID_SAMPLE = 200


def _competitions_by_participant(db) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = defaultdict(set)
    for r in db.query("SELECT competition_id, home_id, away_id FROM canonical_events"):
        out[r["home_id"]].add(r["competition_id"])
        out[r["away_id"]].add(r["competition_id"])
    return out


def _key_describes(key: str, name: str, sport: str, competitions) -> bool:
    for comp in list(competitions) or [""]:
        if key == canonical_team(name, sport, comp) or same_participant(key, name, sport, comp):
            return True
    return False


def _related(a: str, b: str, sport: str, competitions) -> bool:
    return any(related_names(a, b, sport, comp) for comp in (list(competitions) or [""]))


def audit(db) -> Dict:
    """Read-only. Returns {"delete": {table: [rowid,...]}, "report": {...counts...}}."""
    names = {r["id"]: (r["sport"], r["name"]) for r in db.query("SELECT id, sport, name FROM canonical_participants")}
    comps = _competitions_by_participant(db)
    aliases = defaultdict(set)
    for r in db.query("SELECT sport, alias, canonical_id FROM participant_aliases"):
        aliases[r["canonical_id"]].add(r["alias"])

    marks = ",".join("?" * len(CURATED_ALIAS_SOURCES))
    curated = {r["canonical_id"] for r in db.query(
        f"SELECT DISTINCT canonical_id FROM provider_identity WHERE kind='participant' AND provider IN ({marks})",
        CURATED_ALIAS_SOURCES)}
    bad_aliases: List[int] = []
    for r in db.query("SELECT rowid, sport, alias, canonical_id FROM participant_aliases"):
        cid = r["canonical_id"]
        if cid in names and cid not in curated and not _related(r["alias"], names[cid][1], r["sport"], comps.get(cid, ())):
            bad_aliases.append(r["rowid"])
            aliases[cid].discard(r["alias"])     # an untrusted alias cannot vouch for a mapping below

    stale_refs: List[int] = []
    for r in db.query("SELECT rowid, provider_id, scope, canonical_id FROM provider_identity "
                      "WHERE kind='participant' AND provider_id LIKE 'name:%'"):
        cid = r["canonical_id"]
        if cid not in names:
            continue
        sport, name = names[cid]
        key = r["provider_id"][len("name:"):]
        if key in aliases[cid] or _key_describes(key, name, sport, comps.get(cid, ())):
            continue
        stale_refs.append(r["rowid"])

    # A canonical event is contaminated when a stored prediction for it names a side that is not that event's
    # participant (a provider game folded into another game). Names are compared with the lenient related test so
    # legitimate spellings ("Troyes" / "ESTAC Troyes") never count.
    events = {r["id"]: r for r in db.query("SELECT * FROM canonical_events")}
    contaminated: Set[str] = set()
    picks = db.query("SELECT canonical_event_id, sport, home, away FROM picks "
                     "WHERE canonical_event_id IS NOT NULL AND canonical_event_id<>''")
    for p in picks:
        ev = events.get(p["canonical_event_id"])
        if not ev or ev["home_id"] not in names or ev["away_id"] not in names:
            continue
        sport = p["sport"]
        ch, ca = names[ev["home_id"]][1], names[ev["away_id"]][1]
        comp = (ev["competition_id"],)
        same = _related(p["home"], ch, sport, comp) and _related(p["away"], ca, sport, comp)
        swapped = _related(p["home"], ca, sport, comp) and _related(p["away"], ch, sport, comp)
        if not (same or swapped):
            contaminated.add(p["canonical_event_id"])

    event_maps: List[int] = []
    for eid in contaminated:
        event_maps += [r["rowid"] for r in db.query("SELECT rowid FROM provider_identity WHERE kind='event' "
                                                     "AND canonical_id=?", (eid,))]

    multi = db.query("SELECT COUNT(*) AS n FROM (SELECT provider, canonical_id FROM provider_identity WHERE kind='event' "
                     "GROUP BY provider, canonical_id HAVING COUNT(DISTINCT provider_id) > 1)")[0]["n"]

    # A pick that names a side the participant id behind it is not: deterministic evidence that this participant's
    # history (and the Elo update its settlement made) includes another team's game.
    misattributed: Set[str] = set()
    mis_pending = mis_settled = 0
    settled_on: Set[str] = set()
    try:
        rows = db.query("SELECT sport, home, away, home_id, away_id, status FROM picks "
                        "WHERE home_id IS NOT NULL AND home_id<>'' AND away_id IS NOT NULL AND away_id<>''")
    except sqlite3.Error:
        rows = []
    # the spellings a participant is VALIDLY known by: its own key, trusted aliases, and provider name mappings that
    # still describe it (the stale ones found above are exactly the collapsed spellings)
    stale = set(stale_refs)
    valid_keys: Dict[str, Set[str]] = defaultdict(set)
    for r in db.query("SELECT rowid, provider_id, canonical_id FROM provider_identity "
                      "WHERE kind='participant' AND provider_id LIKE 'name:%'"):
        if r["rowid"] not in stale:
            valid_keys[r["canonical_id"]].add(r["provider_id"][len("name:"):])
    for cid, keys in aliases.items():
        valid_keys[cid] |= keys

    def misnamed(name, pid, sport):
        if pid not in names:
            return False
        canonical = names[pid][1]
        for comp in list(comps.get(pid, ())) or [""]:
            if canonical_team(name, sport, comp) in valid_keys[pid] | {canonical_team(canonical, sport, comp)}                     or same_participant(name, canonical, sport, comp):
                return False
        return True

    for p in rows:
        wrong = [pid for side, pid in (("home", p["home_id"]), ("away", p["away_id"]))
                 if misnamed(p[side], pid, p["sport"])]
        if not wrong:
            continue
        misattributed.update(wrong)
        if p["status"] == "pending":
            mis_pending += 1
        else:
            mis_settled += 1
            settled_on.update(wrong)
    stale_participants = {r["canonical_id"] for r in db.query("SELECT rowid, canonical_id FROM provider_identity")
                          if r["rowid"] in set(stale_refs)}
    alias_participants = {r["canonical_id"] for r in db.query("SELECT rowid, canonical_id FROM participant_aliases")
                          if r["rowid"] in set(bad_aliases)}

    affected_participants = {events[e][side] for e in contaminated for side in ("home_id", "away_id")}
    touched = affected_participants | stale_participants | alias_participants | misattributed
    rated = {r["team"] for r in db.query("SELECT team FROM ratings")} if _has_table(db, "ratings") else set()
    marks = ",".join("?" * len(contaminated)) or "''"
    pmarks = ",".join("?" * len(affected_participants)) or "''"
    ev_args, p_args = tuple(contaminated), tuple(affected_participants)

    def count(sql, args):
        try:
            return db.query(sql, args)[0]["n"]
        except sqlite3.Error:
            return 0

    report = {
        "stale_participant_mappings": len(stale_refs),
        "untrusted_aliases": len(bad_aliases),
        "contaminated_events": len(contaminated),
        "event_mappings_on_contaminated_events": len(event_maps),
        "events_with_several_ids_from_one_provider": multi,
        "picks_on_contaminated_events": count(f"SELECT COUNT(*) AS n FROM picks WHERE canonical_event_id IN ({marks})", ev_args),
        "pending_picks_on_contaminated_events": count(
            f"SELECT COUNT(*) AS n FROM picks WHERE status='pending' AND canonical_event_id IN ({marks})", ev_args),
        "settled_picks_on_contaminated_events": count(
            f"SELECT COUNT(*) AS n FROM picks WHERE status<>'pending' AND canonical_event_id IN ({marks})", ev_args),
        "memberships_on_contaminated_events": count(
            f"SELECT COUNT(*) AS n FROM prediction_memberships m JOIN picks p ON p.id=m.prediction_id "
            f"WHERE p.canonical_event_id IN ({marks})", ev_args),
        "custom_candidates_on_contaminated_events": count(
            f"SELECT COUNT(*) AS n FROM custom_pick_candidates WHERE canonical_event_id IN ({marks})", ev_args),
        "participants_on_contaminated_events": len(affected_participants),
        "elo_ratings_of_those_participants": count(f"SELECT COUNT(*) AS n FROM ratings WHERE team IN ({pmarks})", p_args),
        "match_results_of_those_participants": count(
            f"SELECT COUNT(*) AS n FROM match_results WHERE home_id IN ({pmarks}) OR away_id IN ({pmarks})", p_args + p_args),
        "participants_with_wrong_lookup_rows": len(stale_participants | alias_participants),
        "participants_with_misattributed_picks": len(misattributed),
        "misattributed_pending_picks": mis_pending,
        "misattributed_settled_picks": mis_settled,
        "participants_possibly_touched": len(touched),
        "ratings_deterministically_contaminated": len(settled_on & rated),
        "ratings_possibly_contaminated": len((touched - settled_on) & rated),
    }
    ids = {
        "contaminated_event_ids": sorted(contaminated)[:ID_SAMPLE],
        "participants_possibly_touched_ids": sorted(touched)[:ID_SAMPLE],
        "ratings_deterministically_contaminated_ids": sorted(settled_on & rated)[:ID_SAMPLE],
        "ratings_possibly_contaminated_ids": sorted((touched - settled_on) & rated)[:ID_SAMPLE],
    }
    return {"delete": {"provider_identity": sorted(set(stale_refs) | set(event_maps)),
                       "participant_aliases": sorted(bad_aliases)},
            "report": report, "ids": ids}


def _has_table(db, name: str) -> bool:
    return bool(db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def backup(db, path: str) -> None:
    """Consistent online copy (SQLite backup API; never a raw file copy of a WAL database), verified readable."""
    if os.path.exists(path):
        raise ValueError("backup path already exists")
    with db._lock:
        dst = sqlite3.connect(path)
        try:
            db.conn.backup(dst)
        finally:
            dst.close()
    check = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup failed its integrity check")
    finally:
        check.close()


def repair(db, apply: bool = False, backup_path: str = "") -> Dict:
    """Dry run unless ``apply``. Applying requires ``backup_path`` (written and verified first). Idempotent: a second
    run finds nothing to delete. Returns the audit report plus what was deleted."""
    found = audit(db)
    plan = {table: len(rows) for table, rows in found["delete"].items()}
    result = {"dry_run": not apply, "would_delete" if not apply else "deleted": plan, "report": found["report"],
              "ids": found["ids"]}
    log.info("identity repair %s | %s | %s", "APPLY" if apply else "dry run", json.dumps(plan, sort_keys=True),
             json.dumps(found["report"], sort_keys=True))
    if not apply or not any(plan.values()):
        return result
    if not backup_path:
        raise ValueError("refusing to apply without a backup path")
    backup(db, backup_path)
    with db.transaction():
        for table, rowids in found["delete"].items():
            for rowid in rowids:
                db.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
    result["backup"] = "written and verified"
    return result
