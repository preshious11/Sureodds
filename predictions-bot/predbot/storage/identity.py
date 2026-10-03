"""Durable canonical participant, competition and event resolution."""
from __future__ import annotations

import hashlib
from collections import Counter

from predbot.markets.competitions import competition_for_event
from predbot.util.fixtures import fixture_key
from predbot.util.names import canonical_team, same_team
from predbot.util.timeutil import iso, parse_iso


def _id(namespace, value):
    return namespace + ":" + hashlib.sha256(value.encode()).hexdigest()[:24]


class IdentityRegistry:
    def __init__(self, db):
        self.db = db

    def find_participant(self, provider, sport, name, provider_id=""):
        """The canonical participant this provider name already resolves to, or ``None`` when it is new.

        Order: this provider's stored mapping (provider id when it supplies one), a learned alias, an exact
        normalised-name match, then fuzzy matching. An ambiguous fuzzy match is rejected, never guessed."""
        if not str(name or "").strip():
            raise ValueError("unresolved_participant")
        reference = str(provider_id or "name:" + canonical_team(name, sport))
        rows = self.db.query("SELECT canonical_id FROM provider_identity WHERE provider=? AND kind='participant' "
                             "AND provider_id=? AND scope=?", (provider, reference, sport))
        if rows:
            return rows[0]["canonical_id"]
        alias = self.db.query("SELECT canonical_id FROM participant_aliases WHERE sport=? AND alias=?",
                              (sport, canonical_team(name, sport)))
        if alias:
            return alias[0]["canonical_id"]
        candidates = self.db.query("SELECT id,name FROM canonical_participants WHERE sport=?", (sport,))
        exact = [r for r in candidates if canonical_team(r["name"], sport) == canonical_team(name, sport)]
        matched = exact or [r for r in candidates if same_team(name, r["name"], sport=sport)]
        if len(matched) > 1:
            raise ValueError("unresolved_participant")
        return matched[0]["id"] if matched else None

    def _record_participant(self, provider, sport, name, provider_id, cid, alias=False):
        reference = str(provider_id or "name:" + canonical_team(name, sport))
        self.db.execute("INSERT OR IGNORE INTO canonical_participants VALUES(?,?,?)", (cid, sport, name))
        self.db.execute("INSERT OR IGNORE INTO provider_identity VALUES(?,?,?,?,?)",
                        (provider, "participant", reference, sport, cid))
        if alias:
            self.db.execute("INSERT OR IGNORE INTO participant_aliases VALUES(?,?,?)",
                            (sport, canonical_team(name, sport), cid))

    def add_alias(self, sport, name, cid):
        """Record ``name`` as another spelling of participant ``cid``. Never overrides an existing alias, and never
        claims a name that already belongs to a different participant. Returns whether the alias now points at ``cid``."""
        key = canonical_team(name, sport)
        if not key:
            return False
        existing = self.db.query("SELECT canonical_id FROM participant_aliases WHERE sport=? AND alias=?", (sport, key))
        if existing:
            return existing[0]["canonical_id"] == cid
        for r in self.db.query("SELECT id,name FROM canonical_participants WHERE sport=?", (sport,)):
            if r["id"] != cid and canonical_team(r["name"], sport) == key:
                return False
        self.db.execute("INSERT OR IGNORE INTO participant_aliases VALUES(?,?,?)", (sport, key, cid))
        return True

    def participant(self, provider, sport, name, provider_id=""):
        cid = self.find_participant(provider, sport, name, provider_id)
        if cid is None:
            cid = _id(sport + ":participant", canonical_team(name, sport))
        self._record_participant(provider, sport, name, provider_id, cid)
        return cid

    def _opponent_of(self, competition, anchor, start):
        """The other side of the one known fixture in ``competition`` that ``anchor`` plays near ``start``.

        A club cannot play two matches of one competition within the kickoff window, so when one side of an
        incoming event is already a known participant and a canonical event for it exists at that time, the
        unrecognised other side is the same club under another spelling ("ESTAC Troyes" / "Troyes").
        ``None`` unless exactly one such fixture exists."""
        rows = [r for r in self.db.query("SELECT * FROM canonical_events WHERE competition_id=? "
                                         "AND (home_id=? OR away_id=?)", (competition, anchor, anchor))
                if abs((parse_iso(r["kickoff_utc"]) - start).total_seconds()) <= 10800]
        if len(rows) != 1:
            return None
        return rows[0]["away_id"] if rows[0]["home_id"] == anchor else rows[0]["home_id"]

    def resolve(self, event, supported=()):
        comp = competition_for_event(event)
        raw_comp = str(event.league_id or event.extra.get("sport_key") or event.league)
        mapping = self.db.query("SELECT canonical_id FROM provider_identity WHERE provider=? AND kind='competition' "
                                "AND provider_id=? AND scope=?", (event.provider, raw_comp, event.sport))
        cid = mapping[0]["canonical_id"] if mapping else comp.id if comp else event.canonical_competition_id
        if not cid or supported and cid not in supported:
            raise ValueError("unsupported_competition")
        self.db.execute("INSERT OR IGNORE INTO provider_identity VALUES(?,?,?,?,?)",
                        (event.provider, "competition", raw_comp, event.sport, cid))
        ids = event.extra.get("participant_ids") or {}
        home = self.find_participant(event.provider, event.sport, event.home, ids.get("home", ""))
        away = self.find_participant(event.provider, event.sport, event.away, ids.get("away", ""))
        alias_home = alias_away = False
        if event.start and event.start.tzinfo is not None:
            if home and away is None:
                other = self._opponent_of(cid, home, event.start)
                away, alias_away = other, other is not None
            elif away and home is None:
                other = self._opponent_of(cid, away, event.start)
                home, alias_home = other, other is not None
        if home is None:
            home = _id(event.sport + ":participant", canonical_team(event.home, event.sport))
        if away is None:
            away = _id(event.sport + ":participant", canonical_team(event.away, event.sport))
        self._record_participant(event.provider, event.sport, event.home, ids.get("home", ""), home, alias_home)
        self._record_participant(event.provider, event.sport, event.away, ids.get("away", ""), away, alias_away)
        if home == away or not event.start or event.start.tzinfo is None:
            raise ValueError("unresolved_participant")
        mapping = self.db.query("SELECT canonical_id FROM provider_identity WHERE provider=? AND kind='event' "
                                "AND provider_id=? AND scope=?", (event.provider, event.provider_event_id, cid))
        candidates = self.db.query("SELECT * FROM canonical_events WHERE competition_id=? "
                                   "AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))",
                                   (cid, home, away, away, home))
        matches = [r for r in candidates if abs((parse_iso(r["kickoff_utc"]) - event.start).total_seconds()) <= 10800]
        if len(matches) > 1 and not mapping:
            raise ValueError("ambiguous_event")
        eid = (mapping[0]["canonical_id"] if mapping else matches[0]["id"] if matches
               else _id("event", cid + "|" + "|".join(sorted((home, away))) + "|" + iso(event.start)))
        self.db.execute("INSERT OR IGNORE INTO canonical_events VALUES(?,?,?,?,?)", (eid, cid, home, away, iso(event.start)))
        self.db.execute("INSERT OR IGNORE INTO provider_identity VALUES(?,?,?,?,?)",
                        (event.provider, "event", event.provider_event_id, cid, eid))
        event.canonical_event_id, event.canonical_competition_id = eid, cid
        event.home_id, event.away_id = home, away
        return event


def resolve_events_detailed(events, db, supported=()):
    """(kept, failed) where ``failed`` is a list of (event, reason). One entry per provider attempt."""
    registry, kept, failed = IdentityRegistry(db), [], []
    with db.transaction():
        for event in events:
            if event.canonical_event_id:           # already resolved upstream (collection): idempotent
                kept.append(event)
                continue
            try:
                kept.append(registry.resolve(event, supported))
            except ValueError as exc:
                failed.append((event, str(exc)))
    return kept, failed


def resolve_events(events, db, supported=()):
    """(kept, {reason: provider-attempt count}). Use ``unique_failed_fixtures`` for canonical-event counts."""
    kept, failed = resolve_events_detailed(events, db, supported)
    return kept, dict(Counter(reason for _, reason in failed))


def failure_record(event, reason):
    """What a report keeps about an unresolved event: enough to count fixtures once, not provider attempts."""
    return {"key": fixture_key(event), "reason": reason, "sport": event.sport, "home": event.home,
            "away": event.away, "start": iso(event.start) if event.start else ""}


def unique_failed_fixtures(records, kept_events):
    """{fixture key: reason} for fixtures that failed and were not resolved by ANY provider.

    A fixture failing through two providers counts once; one that failed through provider A but resolved through
    provider B is not a failed fixture at all."""
    resolved = {fixture_key(e) for e in kept_events}
    out = {}
    for r in records:
        if r["key"] in resolved or r["key"] in out:
            continue
        out[r["key"]] = r["reason"]
    return out
