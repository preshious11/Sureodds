"""Durable canonical participant, competition and event resolution."""
from __future__ import annotations

import hashlib
from collections import Counter

from predbot.markets.competitions import competition_for_event
from predbot.util.fixtures import SERIES_KICKOFF_WINDOW_SECONDS, fixture_key, kickoff_window
from predbot.util.names import canonical_team, related_names, same_participant
from predbot.util.timeutil import iso, parse_iso


#: Sources whose aliases are curated rather than inferred: football-data.org supplies each team's own name, short
#: name and code under its team id; the odds-history link learns a spelling only from repeated consistent matches.
CURATED_ALIAS_SOURCES = ("football_data", "football_data_co_uk")


def _id(namespace, value):
    return namespace + ":" + hashlib.sha256(value.encode()).hexdigest()[:24]


class IdentityRegistry:
    """Canonical participants and events.

    The rule throughout: when the evidence is ambiguous, two participants or events stay SEPARATE. A duplicate row is
    a cosmetic problem; merging two real teams or games publishes a fixture that does not exist and settles it
    against the wrong result. So:

    * names match only under the strict ``same_participant`` test, scoped to the competition;
    * a stored provider mapping (participant or event) is trusted only while it is consistent with what the
      provider says now - a mapping written by an earlier, looser rule is ignored instead of propagated;
    * a new spelling is learned as a permanent alias only with fixture corroboration (same competition, same known
      opponent, kickoff within the sport's window, the fixture not already claimed by this provider under another
      event id) AND name evidence (``related_names``) - context alone never teaches an alias;
    * a provider's own event ids are authoritative between its events when their kickoffs are apart: two ids from
      one provider hours apart are two games (a doubleheader). At the SAME kickoff they cannot be two games for
      the same team - providers do list one match twice under two spellings - so that is decided by the names.
    """

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------------ participants
    def _names_of(self, cid):
        """Every spelling known for participant ``cid``: its canonical name and learned alias keys."""
        names = [r["name"] for r in self.db.query("SELECT name FROM canonical_participants WHERE id=?", (cid,))]
        names += [r["alias"] for r in self.db.query("SELECT alias FROM participant_aliases WHERE canonical_id=?", (cid,))]
        return names

    def _consistent(self, cid, sport, name, competition=""):
        """Whether the stored participant ``cid`` can be the team ``name``: a strict match with one of its known
        spellings. An unknown ``cid`` (no stored name) is accepted."""
        names = self._names_of(cid)
        key = canonical_team(name, sport, competition)
        return not names or any(n == key or same_participant(name, n, sport, competition) for n in names)

    def find_participant(self, provider, sport, name, provider_id="", competition=""):
        """The canonical participant this provider name already resolves to, or ``None`` when it is new.

        Order: this provider's stored mapping (provider id when it supplies one) if still consistent with the name,
        a learned alias, an exact canonical-key match, then the strict ``same_participant`` match. More than one
        candidate is ambiguous and rejected, never guessed. Fuzzy resemblance alone never resolves a participant."""
        if not str(name or "").strip():
            raise ValueError("unresolved_participant")
        key = canonical_team(name, sport, competition)
        reference = str(provider_id or "name:" + key)
        rows = self.db.query("SELECT canonical_id FROM provider_identity WHERE provider=? AND kind='participant' "
                             "AND provider_id=? AND scope=?", (provider, reference, sport))
        if rows and self._consistent(rows[0]["canonical_id"], sport, name, competition):
            return rows[0]["canonical_id"]
        alias = self.db.query("SELECT canonical_id FROM participant_aliases WHERE sport=? AND alias=?", (sport, key))
        if alias and self._alias_trusted(alias[0]["canonical_id"], sport, name, competition):
            return alias[0]["canonical_id"]
        candidates = self.db.query("SELECT id,name FROM canonical_participants WHERE sport=?", (sport,))
        exact = {r["id"] for r in candidates if canonical_team(r["name"], sport, competition) == key}
        matched = exact or {r["id"] for r in candidates if same_participant(name, r["name"], sport, competition)}
        if len(matched) > 1:
            raise ValueError("unresolved_participant")
        return next(iter(matched)) if matched else None

    def _alias_trusted(self, cid, sport, name, competition=""):
        """An alias row is used while it has name evidence for its participant or came from a curated source (a
        club code such as "BVB" has no textual link to "Borussia Dortmund"). An alias that an older, looser rule
        inferred from fixture context alone ("basket" -> Milano) is ignored instead of followed."""
        canonical = [r["name"] for r in self.db.query("SELECT name FROM canonical_participants WHERE id=?", (cid,))]
        if not canonical or any(related_names(name, n, sport, competition) for n in canonical):
            return True
        marks = ",".join("?" * len(CURATED_ALIAS_SOURCES))
        return bool(self.db.query(f"SELECT 1 FROM provider_identity WHERE kind='participant' AND canonical_id=? "
                                  f"AND provider IN ({marks}) LIMIT 1", (cid,) + CURATED_ALIAS_SOURCES))

    def _record_participant(self, provider, sport, name, provider_id, cid, alias=False, competition=""):
        key = canonical_team(name, sport, competition)
        reference = str(provider_id or "name:" + key)
        self.db.execute("INSERT OR IGNORE INTO canonical_participants VALUES(?,?,?)", (cid, sport, name))
        self.db.execute("INSERT OR IGNORE INTO provider_identity VALUES(?,?,?,?,?)",
                        (provider, "participant", reference, sport, cid))
        if alias:
            self.db.execute("INSERT OR IGNORE INTO participant_aliases VALUES(?,?,?)", (sport, key, cid))

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

    def participant(self, provider, sport, name, provider_id="", competition=""):
        cid = self.find_participant(provider, sport, name, provider_id, competition)
        if cid is None:
            cid = _id(sport + ":participant", canonical_team(name, sport, competition))
        self._record_participant(provider, sport, name, provider_id, cid, competition=competition)
        return cid

    # ------------------------------------------------------------------ events
    def _claimed_elsewhere(self, provider, provider_event_id, eid, gap):
        """Whether ``provider`` itself says canonical event ``eid`` is a DIFFERENT game: it already maps another of
        its event ids to it at a kickoff ``gap`` seconds away - more than a team could be two listings of one match
        (``SERIES_KICKOFF_WINDOW_SECONDS``). A doubleheader's second game is never folded into the first."""
        if gap <= SERIES_KICKOFF_WINDOW_SECONDS:
            return False
        return bool(self.db.query("SELECT 1 FROM provider_identity WHERE provider=? AND kind='event' AND canonical_id=? "
                                  "AND provider_id<>? LIMIT 1", (provider, eid, provider_event_id)))

    def _opponent_of(self, event, competition, anchor, unknown_name, window):
        """The participant behind ``unknown_name`` when it is the other side of the ONE known fixture that ``anchor``
        plays in ``competition`` near the kickoff (a club cannot play two matches of one competition at once): the
        "ESTAC Troyes" / "Troyes" case. ``None`` unless exactly one such fixture exists, it is not claimed by this
        provider under another event id, and the names are related (``related_names``)."""
        rows = []
        for r in self.db.query("SELECT * FROM canonical_events WHERE competition_id=? AND (home_id=? OR away_id=?)",
                               (competition, anchor, anchor)):
            gap = abs((parse_iso(r["kickoff_utc"]) - event.start).total_seconds())
            if gap <= window and not self._claimed_elsewhere(event.provider, event.provider_event_id, r["id"], gap):
                rows.append(r)
        if len(rows) != 1:
            return None
        other = rows[0]["away_id"] if rows[0]["home_id"] == anchor else rows[0]["home_id"]
        names = [r["name"] for r in self.db.query("SELECT name FROM canonical_participants WHERE id=?", (other,))]
        if not any(related_names(unknown_name, n, event.sport, competition) for n in names):
            return None
        return other

    def resolve(self, event, supported=(), series=False):
        """Canonical competition, participants and event for one provider event.

        ``series``: this provider lists the same pairing more than once around this time (a doubleheader), so a
        kickoff must match closely to be the same game."""
        comp = competition_for_event(event)
        raw_comp = str(event.league_id or event.extra.get("sport_key") or event.league)
        mapping = self.db.query("SELECT canonical_id FROM provider_identity WHERE provider=? AND kind='competition' "
                                "AND provider_id=? AND scope=?", (event.provider, raw_comp, event.sport))
        cid = mapping[0]["canonical_id"] if mapping else comp.id if comp else event.canonical_competition_id
        if not cid or supported and cid not in supported:
            raise ValueError("unsupported_competition")
        self.db.execute("INSERT OR IGNORE INTO provider_identity VALUES(?,?,?,?,?)",
                        (event.provider, "competition", raw_comp, event.sport, cid))
        if not event.start or event.start.tzinfo is None:
            raise ValueError("unresolved_participant")
        window = kickoff_window(event.sport, series)
        ids = event.extra.get("participant_ids") or {}
        home = self.find_participant(event.provider, event.sport, event.home, ids.get("home", ""), cid)
        away = self.find_participant(event.provider, event.sport, event.away, ids.get("away", ""), cid)
        alias_home = alias_away = False
        if home and away is None:
            other = self._opponent_of(event, cid, home, event.away, window)
            away, alias_away = other, other is not None
        elif away and home is None:
            other = self._opponent_of(event, cid, away, event.home, window)
            home, alias_home = other, other is not None
        if home is None:
            home = _id(event.sport + ":participant", canonical_team(event.home, event.sport, cid))
        if away is None:
            away = _id(event.sport + ":participant", canonical_team(event.away, event.sport, cid))
        if home == away:
            raise ValueError("unresolved_participant")
        self._record_participant(event.provider, event.sport, event.home, ids.get("home", ""), home, alias_home, cid)
        self._record_participant(event.provider, event.sport, event.away, ids.get("away", ""), away, alias_away, cid)
        pair = {home, away}
        mapped = [r for r in self.db.query(
            "SELECT e.* FROM provider_identity p JOIN canonical_events e ON e.id=p.canonical_id WHERE p.provider=? "
            "AND p.kind='event' AND p.provider_id=? AND p.scope=?", (event.provider, event.provider_event_id, cid))
            # a mapping is trusted only while it describes the same two teams (an older, looser rule may have
            # attached this provider event to a different game)
            if {r["home_id"], r["away_id"]} == pair]
        if mapped:
            eid = mapped[0]["id"]
        else:
            candidates = self.db.query("SELECT * FROM canonical_events WHERE competition_id=? "
                                       "AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))",
                                       (cid, home, away, away, home))
            near = sorted(((abs((parse_iso(r["kickoff_utc"]) - event.start).total_seconds()), r["id"])
                           for r in candidates),
                          key=lambda x: x[0])
            near = [(gap, rid) for gap, rid in near if gap <= window
                    and not self._claimed_elsewhere(event.provider, event.provider_event_id, rid, gap)]
            if len(near) > 1 and near[0][0] == near[1][0]:
                raise ValueError("ambiguous_event")
            eid = (near[0][1] if near
                   else _id("event", cid + "|" + "|".join(sorted((home, away))) + "|" + iso(event.start)))
        self.db.execute("INSERT OR IGNORE INTO canonical_events VALUES(?,?,?,?,?)", (eid, cid, home, away, iso(event.start)))
        self.db.execute("INSERT OR IGNORE INTO provider_identity VALUES(?,?,?,?,?)",
                        (event.provider, "event", event.provider_event_id, cid, eid))
        event.canonical_event_id, event.canonical_competition_id = eid, cid
        event.home_id, event.away_id = home, away
        return event


def _series_members(events):
    """ids of events whose provider lists the same pairing (same sport and competition) more than once within six
    hours: doubleheaders and same-day series."""
    groups = {}
    for e in events:
        if not e.start or e.canonical_event_id:
            continue
        comp = competition_for_event(e)
        comp_id = comp.id if comp else str(e.league_id or e.league)
        pair = frozenset((canonical_team(e.home, e.sport, comp_id), canonical_team(e.away, e.sport, comp_id)))
        groups.setdefault((e.provider, e.sport, comp_id, pair), []).append(e)
    out = set()
    for members in groups.values():
        for a in members:
            if any(b is not a and b.provider_event_id != a.provider_event_id
                   and abs((a.start - b.start).total_seconds()) <= 6 * 3600 for b in members):
                out.add(id(a))
    return out


def resolve_events_detailed(events, db, supported=()):
    """(kept, failed) where ``failed`` is a list of (event, reason). One entry per provider attempt."""
    registry, kept, failed = IdentityRegistry(db), [], []
    series = _series_members(events)
    with db.transaction():
        for event in events:
            if event.canonical_event_id:           # already resolved upstream (collection): idempotent
                kept.append(event)
                continue
            try:
                kept.append(registry.resolve(event, supported, series=id(event) in series))
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
