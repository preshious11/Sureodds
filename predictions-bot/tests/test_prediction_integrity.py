"""Batch 1 - prediction integrity: the right fixture, the right teams, one market per bet.

C1  different teams sharing a last word must never become one participant / one fixture
M15 a doubleheader is two fixtures; one game listed by two providers is one
M16 a similar-looking name is not the same participant (Nigeria/Niger, Austria/Australia, Real Madrid/Castilla)
H3  SportsGameOdds' per-side entity must not split one market into two (de-vig, sharp reference, margin model)
"""
from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel
from predbot.providers.base import Event, MarketLine
from predbot.providers.budget import Budget
from predbot.providers.chain import merge_events
from predbot.storage.db import Database
from predbot.storage.identity import IdentityRegistry, resolve_events, resolve_events_detailed
from predbot.storage.identity_repair import audit, repair
from predbot.util.fixtures import fixture_key, fixture_orientation
from predbot.util.names import canonical_team, related_names, same_participant

T = datetime(2030, 6, 1, 16, 0, tzinfo=timezone.utc)


def ev(provider, home, away, start=T, league_id="euroleague", sport="basketball", eid=None, markets=None, extra=None):
    return Event(provider=provider, provider_event_id=eid or f"{provider}:{home}:{away}:{start:%H%M}", sport=sport,
                 league=league_id, league_id=league_id, home=home, away=away, start=start,
                 markets=markets if markets is not None else [MarketLine("result2", "home", 1.9)],
                 sources=[provider], extra=extra or {})


def count(db, table, where="1=1", args=()):
    return db.query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)[0]["n"]


# ====================================================================== C1: names never collapse to a last word
@pytest.mark.parametrize("a,b,sport,comp", [
    ("Maccabi Tel Aviv", "Hapoel Tel Aviv", "basketball", "euroleague"),
    ("Kentucky Wildcats", "Arizona Wildcats", "basketball", "ncaab"),
    ("Villanova Wildcats", "Northwestern Wildcats", "basketball", "ncaab"),
    ("Kansas State Wildcats", "Kentucky Wildcats", "american_football", "ncaaf"),
    ("Auburn Tigers", "LSU Tigers", "american_football", "ncaaf"),
    ("Clemson Tigers", "Missouri Tigers", "basketball", "ncaab"),
    ("Penn State Nittany Lions", "Detroit Lions", "american_football", "ncaaf"),
    ("Virtus Bologna", "Fortitudo Bologna", "basketball", "euroleague"),
    ("Real Madrid", "Atletico Madrid", "basketball", "euroleague"),
    ("South Carolina Gamecocks", "Coastal Carolina Chanticleers", "american_football", "ncaaf"),
])
def test_teams_sharing_a_last_word_keep_distinct_identities(a, b, sport, comp):
    assert canonical_team(a, sport, comp) != canonical_team(b, sport, comp)
    assert not same_participant(a, b, sport, comp)


def test_curated_professional_nicknames_still_unify_spellings_inside_their_league():
    assert canonical_team("LA Lakers", "basketball", "nba") == canonical_team("Los Angeles Lakers", "basketball", "nba")
    assert canonical_team("TOR Blue Jays", "baseball", "mlb") == canonical_team("Toronto Blue Jays", "baseball", "mlb") == "jays"
    assert canonical_team("NY Giants", "american_football", "nfl") == canonical_team("New York Giants", "american_football", "nfl")
    # ... but never outside it: a college or EuroLeague name is not reduced to a franchise word
    assert canonical_team("LSU Tigers", "baseball", "ncaab") == "lsu tigers"
    assert canonical_team("Penn State Nittany Lions", "american_football", "ncaaf") == "penn state nittany lions"


@pytest.mark.parametrize("league,teams", [
    ("ncaab", ["Kentucky Wildcats", "Arizona Wildcats", "Villanova Wildcats", "Northwestern Wildcats",
               "Kansas State Wildcats", "Weber State Wildcats"]),
    ("euroleague", ["Maccabi Tel Aviv", "Hapoel Tel Aviv", "Virtus Bologna", "Real Madrid", "Valencia Basket Club",
                    "Olimpia Milano"]),
])
def test_same_mascot_or_city_teams_resolve_to_distinct_participants_and_events(db, league, teams):
    events = [ev("sharpapi", teams[i], teams[i + 1], T + timedelta(minutes=10 * i), league)
              for i in range(0, len(teams), 2)]
    kept, failures = resolve_events(events, db)
    assert not failures and len(kept) == len(events)
    assert len({e.canonical_event_id for e in events}) == len(events)
    assert len({pid for e in events for pid in (e.home_id, e.away_id)}) == len(teams)
    assert len({fixture_key(e) for e in events}) == len(events)


# ====================================================================== C1 replay: Maccabi / Hapoel / Valencia / Milano
def _euroleague_night():
    milano = ev("sharpapi", "Maccabi Tel Aviv", "EA7 Emporio Armani Milano", T,
                eid="euroleague_maccabitelaviv_olimpiamilano_b2", markets=[MarketLine("handicap", "home", 1.9, line=-3.5)])
    hapoel = ev("sharpapi", "Valencia Basket Club", "Hapoel Tel Aviv", T + timedelta(hours=2, minutes=30),
                eid="euroleague_hapoeltelaviv_valencia_b3", markets=[MarketLine("handicap", "away", 1.95, line=1.5)])
    return milano, hapoel


def _assert_two_real_games(db, milano, hapoel):
    assert milano.canonical_event_id != hapoel.canonical_event_id
    assert len({milano.home_id, milano.away_id, hapoel.home_id, hapoel.away_id}) == 4
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM canonical_participants")}
    assert (names[milano.home_id], names[milano.away_id]) == ("Maccabi Tel Aviv", "EA7 Emporio Armani Milano")
    assert (names[hapoel.home_id], names[hapoel.away_id]) == ("Valencia Basket Club", "Hapoel Tel Aviv")
    merged = merge_events([milano], [hapoel])
    assert len(merged) == 2
    by_id = {e.canonical_event_id: e for e in merged}
    assert [(m.selection, m.line) for m in by_id[milano.canonical_event_id].markets] == [("home", -3.5)]
    assert [(m.selection, m.line) for m in by_id[hapoel.canonical_event_id].markets] == [("away", 1.5)]
    assert fixture_key(milano) != fixture_key(hapoel)


def test_the_euroleague_replay_keeps_each_real_game_with_its_own_teams(db):
    milano, hapoel = _euroleague_night()
    kept, failures = resolve_events([milano, hapoel], db)
    assert not failures
    _assert_two_real_games(db, milano, hapoel)
    assert count(db, "participant_aliases") == 0           # nothing learned from context alone
    # the old rule reduced both Tel Aviv clubs to one key - the root of the fake fixture
    assert "Maccabi Tel Aviv".split()[-1] == "Hapoel Tel Aviv".split()[-1]


def test_the_replay_against_the_poisoned_rows_the_old_rules_wrote(db):
    """The exact rows the old resolver persisted for this night (Hapoel mapped to Maccabi's participant through the
    'aviv' key, 'basket' learned as an alias of Milano, and Hapoel/Valencia's provider event attached to the Maccabi
    v Milano game). The resolver must not follow any of them."""
    maccabi, milano_id, game = "basketball:participant:old-aviv", "basketball:participant:old-milano", "event:old-game"
    for pid, name in ((maccabi, "Maccabi Tel Aviv"), (milano_id, "EA7 Emporio Armani Milano")):
        db.execute("INSERT INTO canonical_participants VALUES(?,?,?)", (pid, "basketball", name))
    db.execute("INSERT INTO canonical_events VALUES(?,?,?,?,?)", (game, "euroleague", maccabi, milano_id, "2030-06-01T16:00:00Z"))
    for row in (("sharpapi", "participant", "name:aviv", "basketball", maccabi),
                ("sharpapi", "participant", "name:basket", "basketball", milano_id),
                ("sharpapi", "event", "euroleague_hapoeltelaviv_valencia_b3", "euroleague", game)):
        db.execute("INSERT INTO provider_identity VALUES(?,?,?,?,?)", row)
    db.execute("INSERT INTO participant_aliases VALUES(?,?,?)", ("basketball", "basket", milano_id))
    milano, hapoel = _euroleague_night()
    resolve_events([milano, hapoel], db)
    assert milano.canonical_event_id == game and (milano.home_id, milano.away_id) == (maccabi, milano_id)
    assert hapoel.canonical_event_id != game and not {hapoel.home_id, hapoel.away_id} & {maccabi, milano_id}
    _assert_two_real_games(db, milano, hapoel)


# ====================================================================== M16: similar names are different participants
@pytest.mark.parametrize("a,b,sport", [
    ("Nigeria", "Niger", "football"), ("Austria", "Australia", "football"),
    ("Real Madrid", "Real Madrid Castilla", "football"), ("Barcelona", "Barcelona B", "football"),
    ("Karolina Pliskova", "Kristyna Pliskova", "tennis"), ("Kentucky", "Western Kentucky", "american_football"),
    ("Michigan", "Michigan State Spartans", "american_football"), ("Texas", "Texas A&M Aggies", "american_football"),
    ("Manchester United", "Manchester City", "football"),
])
def test_plausible_but_different_names_are_not_the_same_participant(a, b, sport):
    assert not same_participant(a, b, sport)
    assert not same_participant(b, a, sport)


def test_similar_national_teams_resolve_to_separate_participants(db):
    a = ev("sharpapi", "Nigeria", "Ghana", T, "world_-_international_friendlies", "football")
    b = ev("oddsapi", "Niger", "Togo", T, "world_-_international_friendlies", "football")
    c = ev("sportsgameodds", "Austria", "Wales", T, "world_-_international_friendlies", "football")
    d = ev("sharpapi", "Australia", "Japan", T, "world_-_international_friendlies", "football")
    resolve_events([a, b, c, d], db)
    assert a.home_id != b.home_id and c.home_id != d.home_id
    assert len({e.canonical_event_id for e in (a, b, c, d)}) == 4


def test_a_reserve_team_resolves_separately_from_its_first_team(db):
    a = ev("sharpapi", "Real Madrid", "Sevilla", T, "spain_-_la_liga", "football")
    b = ev("oddsapi", "Real Madrid Castilla", "Sevilla", T + timedelta(days=1), "spain_-_la_liga", "football")
    resolve_events([a, b], db)
    assert a.home_id != b.home_id


@pytest.mark.parametrize("a,b,sport", [
    ("Arsenal", "Arsenal FC", "football"), ("Bayer 04 Leverkusen", "Bayer Leverkusen", "football"),
    ("Olympiakos", "Olympiacos", "football"), ("Sheffield Weds", "Sheffield Wednesday", "football"),
    ("Pliskova K.", "Karolina Pliskova", "tennis"), ("Ohio State", "Ohio State Buckeyes", "american_football"),
    ("Valencia Basket Club", "Valencia Basket", "basketball"),
])
def test_genuine_spelling_variants_are_still_one_participant(a, b, sport):
    assert same_participant(a, b, sport)


# ====================================================================== cross-provider merging is preserved
def test_one_real_fixture_from_two_providers_still_merges(db):
    a = ev("sharpapi", "Bayer Leverkusen", "Borussia Dortmund", T, "germany_-_bundesliga", "football")
    b = ev("oddsapi", "Bayer 04 Leverkusen", "Borussia Dortmund", T + timedelta(minutes=5), "germany_-_bundesliga",
           "football", extra={"sport_key": "soccer_germany_bundesliga"})
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id and (a.home_id, a.away_id) == (b.home_id, b.away_id)
    assert len(merge_events([a], [b])) == 1


def test_a_tennis_match_with_reordered_names_merges(db):
    a = ev("sharpapi", "Karolina Pliskova", "Marie Bouzkova", T, "wta", "tennis")
    b = ev("sportsgameodds", "Pliskova K.", "Bouzkova M.", T + timedelta(minutes=3), "wta", "tennis")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id


def test_swapped_home_and_away_resolve_to_the_same_event_and_orientation(db):
    a = ev("sharpapi", "Kentucky Wildcats", "Arizona Wildcats", T, "ncaab")
    b = ev("sportsgameodds", "Arizona Wildcats", "Kentucky Wildcats", T + timedelta(minutes=5), "NCAAB")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id and (a.home_id, a.away_id) == (b.away_id, b.home_id)
    assert fixture_orientation(a, b) == "swapped"
    (m,) = merge_events([a], [b])
    assert m.extra["merged"][0]["swapped"] is True


# ====================================================================== provider event identity / doubleheaders
def _mlb(provider, start, eid, home="New York Yankees", away="Boston Red Sox"):
    return ev(provider, home, away, start, "MLB", "baseball", eid=eid)


def test_two_event_ids_from_one_provider_hours_apart_are_two_fixtures(db):
    g1 = ev("sharpapi", "Le Mans", "Lorient", T, "france_-_ligue_1", "football", eid="lm1")
    g2 = ev("sharpapi", "Le Mans", "Lorient", T + timedelta(hours=2, minutes=30), "france_-_ligue_1", "football", eid="lm2")
    resolve_events([g1, g2], db)
    assert g1.canonical_event_id != g2.canonical_event_id


def test_one_provider_listing_one_match_twice_at_the_same_kickoff_is_one_fixture(db):
    """SharpAPI lists some matches twice (two books' spellings) at the same kickoff: one game, one fixture."""
    a = ev("sharpapi", "Angers", "ESTAC Troyes", T, "france_-_ligue_1", "football", eid="angers_estactroyes")
    b = ev("sharpapi", "Angers", "Troyes", T, "france_-_ligue_1", "football", eid="angers_troyes")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_a_doubleheader_from_two_providers_is_two_fixtures_in_any_arrival_order(tmp_path, order):
    db = Database(str(tmp_path / "dh.db"))
    events = [_mlb("sharpapi", T + timedelta(minutes=5), "s1"), _mlb("sharpapi", T + timedelta(hours=3, minutes=10), "s2"),
              _mlb("sportsgameodds", T, "g1"), _mlb("sportsgameodds", T + timedelta(hours=3, minutes=5), "g2")]
    batch = [events[i] for i in order]
    kept, failures = resolve_events(batch, db)
    assert not failures
    s1, s2, g1, g2 = events
    assert s1.canonical_event_id == g1.canonical_event_id != s2.canonical_event_id == g2.canonical_event_id
    assert count(db, "canonical_events") == 2


def test_doubleheader_games_seen_through_different_providers_are_not_merged(db):
    first = _mlb("sharpapi", T, "s1")
    second = _mlb("sportsgameodds", T + timedelta(hours=2, minutes=55), "g2")
    resolve_events([first, second], db)
    assert first.canonical_event_id != second.canonical_event_id


def test_a_genuine_provider_kickoff_discrepancy_still_merges(db):
    a = _mlb("sharpapi", T, "s1")
    b = _mlb("sportsgameodds", T + timedelta(minutes=40), "g1")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id


def test_a_mapping_to_a_different_game_is_ignored(db):
    """A provider event id already attached (by an older rule) to a game between other teams is not trusted."""
    a = ev("sharpapi", "Real Madrid", "Barcelona", T, "euroleague", eid="x1")
    resolve_events([a], db)
    db.execute("INSERT INTO provider_identity VALUES('oddsapi','event','x2','euroleague',?)", (a.canonical_event_id,))
    b = ev("oddsapi", "Panathinaikos", "Olympiacos", T, "euroleague", eid="x2")
    resolve_events([b], db)
    assert b.canonical_event_id != a.canonical_event_id


# ====================================================================== alias learning
def test_context_alone_never_teaches_an_alias(db):
    known = ev("sharpapi", "Maccabi Tel Aviv", "Olimpia Milano", T)
    resolve_events([known], db)
    other = ev("oddsapi", "Maccabi Tel Aviv", "Valencia Basket Club", T + timedelta(minutes=10))
    resolve_events([other], db)
    assert other.away_id != known.away_id and other.canonical_event_id != known.canonical_event_id
    assert count(db, "participant_aliases") == 0


def test_a_corroborated_related_spelling_is_learned_and_reused(db):
    a = ev("sharpapi", "Angers", "Troyes", T, "france_-_ligue_1", "football")
    b = ev("sportsgameodds", "Angers", "ESTAC Troyes", T + timedelta(minutes=10), "france_-_ligue_1", "football")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id
    assert count(db, "participant_aliases", "alias='estac troyes'") == 1
    later = ev("oddsapi", "ESTAC Troyes", "Lens", T + timedelta(days=7), "france_-_ligue_1", "football")
    resolve_events([later], db)
    assert later.home_id == a.away_id


def test_related_names_is_the_minimum_name_evidence():
    assert related_names("Troyes", "ESTAC Troyes") and related_names("Lyon FC", "Olympique Lyonnais")
    assert not related_names("Valencia Basket Club", "EA7 Emporio Armani Milano")
    assert not related_names("Valencia Basket Club", "Olimpia Milano")


def test_espn_results_harvest_keeps_college_teams_apart_from_professional_franchises(db):
    reg = IdentityRegistry(db)
    college = reg.participant("espn", "american_football", "Penn State Nittany Lions", competition="ncaaf")
    pro = reg.participant("espn", "american_football", "Detroit Lions", competition="nfl")
    assert college != pro


# ====================================================================== fixture keys
COLLISION_FIXTURES = [
    ("basketball", "euroleague", "Maccabi Tel Aviv", "Olimpia Milano"), ("basketball", "euroleague", "Hapoel Tel Aviv", "Olimpia Milano"),
    ("basketball", "ncaab", "Kentucky Wildcats", "Duke Blue Devils"), ("basketball", "ncaab", "Arizona Wildcats", "Duke Blue Devils"),
    ("basketball", "ncaab", "Villanova Wildcats", "Duke Blue Devils"),
    ("american_football", "ncaaf", "Kentucky", "Tennessee"), ("american_football", "ncaaf", "Western Kentucky", "Middle Tennessee"),
    ("american_football", "ncaaf", "South Carolina", "Alabama"), ("american_football", "ncaaf", "Coastal Carolina", "South Alabama"),
    ("football", "intl_friendlies", "Nigeria", "Ghana"), ("football", "intl_friendlies", "Niger", "Ghana"),
]


def test_distinct_real_fixtures_never_share_a_fixture_key():
    keys = [fixture_key(ev("p", h, a, T, comp, sport), comp) for sport, comp, h, a in COLLISION_FIXTURES]
    assert len(set(keys)) == len(keys)


# ====================================================================== H3: SportsGameOdds side markets
def _nba_lines(tagged: bool):
    def side(s):
        return s if tagged else "all"
    return [MarketLine("result2", "home", 1.30, entity=side("home"), book_prices=[1.30], n_books=1),
            MarketLine("result2", "away", 3.60, entity=side("away"), book_prices=[3.60], n_books=1),
            MarketLine("handicap", "home", 1.91, line=-7.5, entity=side("home"), book_prices=[1.91], n_books=1),
            MarketLine("handicap", "away", 1.91, line=7.5, entity=side("away"), book_prices=[1.91], n_books=1),
            MarketLine("total", "over", 1.91, line=224.5, book_prices=[1.91], n_books=1),
            MarketLine("total", "under", 1.91, line=224.5, book_prices=[1.91], n_books=1)]


def _nba(markets):
    return Event(provider="sportsgameodds", provider_event_id="nba1", sport="basketball", league="NBA", league_id="NBA",
                 home="Boston Celtics", away="Miami Heat", start=T, markets=markets, sources=["sportsgameodds"])


def test_sgo_tagged_and_canonical_side_markets_score_identically(db):
    canonical = {(s.line.market, s.line.selection): s.p_model for s in
                 EventModel(_nba(_nba_lines(False)), Elo(db), Calibrator(db)).score_all()}
    tagged_model = EventModel(_nba(_nba_lines(True)), Elo(db), Calibrator(db))
    tagged = {(s.line.market, s.line.selection): s.p_model for s in tagged_model.score_all()}
    assert canonical == tagged
    assert all(ml.entity == "all" for ml in tagged_model.ev.markets)
    # both sides of the handicap and the moneyline form ONE complete market, so they were de-vigged together
    sides = [ml for ml in tagged_model.ev.markets if ml.market in ("handicap", "result2")]
    assert len({ml.market_key() for ml in sides}) == 2 and all(tagged_model.market_complete.get(ml.key()) for ml in sides)
    assert canonical[("handicap", "away")] == pytest.approx(0.5, abs=0.02)


def test_the_pre_fix_representation_really_split_the_market(db):
    """Guards the regression test itself: forcing the old per-side entity back (bypassing normalisation) splits the
    handicap into two incomplete one-sided markets and moves the away +7.5 probability."""
    lines = _nba_lines(False)
    for ml in lines:
        if ml.market in ("result2", "handicap"):
            object.__setattr__(ml, "entity", ml.selection)
    old = EventModel(_nba(lines), Elo(db), Calibrator(db))
    handicaps = [ml for ml in old.ev.markets if ml.market == "handicap"]
    assert len({ml.market_key() for ml in handicaps}) == 2 and not any(old.market_complete.get(ml.key()) for ml in handicaps)
    fixed = {(s.line.market, s.line.selection): s.p_model for s in
             EventModel(_nba(_nba_lines(True)), Elo(db), Calibrator(db)).score_all()}
    broken = {(s.line.market, s.line.selection): s.p_model for s in old.score_all()}
    assert broken[("handicap", "away")] != pytest.approx(fixed[("handicap", "away")], abs=1e-6)


def test_cached_rows_restored_with_a_side_entity_are_normalised():
    ml = MarketLine(**{"market": "handicap", "selection": "away", "price": 1.9, "line": 7.5, "entity": "away"})
    assert ml.entity == "all" and ml.selection == "away" and ml.line == 7.5
    team_total = MarketLine("total", "over", 1.9, line=110.5, entity="home")
    assert team_total.entity == "home"                       # a team total's entity IS the bet: kept


def test_the_sgo_parser_emits_canonical_side_markets_and_keeps_team_totals(db):
    from predbot.providers.sportsgameodds import SportsGameOddsProvider
    p = SportsGameOddsProvider("k", Budget(db), sports=[], leagues=[])

    def odd(bet, side, entity, **extra):
        return {"statID": "points", "periodID": "game", "betTypeID": bet, "sideID": side, "statEntityID": entity,
                "bookOdds": "-110", "bookOddsAvailable": True, **extra}
    spread = p._parse_odd("a", odd("sp", "away", "away", bookSpread="7.5"), "basketball", {})
    money = p._parse_odd("b", odd("ml", "home", "home"), "basketball", {})
    three = p._parse_odd("c", odd("ml3way", "away", "away"), "football", {})
    team_total = p._parse_odd("d", odd("ou", "over", "home", bookOverUnder="110.5"), "basketball", {})
    assert (spread.market, spread.selection, spread.entity, spread.line) == ("handicap", "away", "all", 7.5)
    assert (money.entity, three.entity) == ("all", "all")
    assert (team_total.market, team_total.entity) == ("total", "home")


# ====================================================================== repair tool
def _poisoned(db):
    db.execute("INSERT INTO canonical_participants VALUES('p:maccabi','basketball','Maccabi Tel Aviv')")
    db.execute("INSERT INTO canonical_participants VALUES('p:milano','basketball','Olimpia Milano')")
    db.execute("INSERT INTO canonical_participants VALUES('p:bvb','football','Borussia Dortmund')")
    db.execute("INSERT INTO canonical_events VALUES('e:game','euroleague','p:maccabi','p:milano','2030-06-01T16:00:00Z')")
    for row in (("sharpapi", "participant", "name:aviv", "basketball", "p:maccabi"),          # old last-word key
                ("sharpapi", "participant", "name:maccabi tel aviv", "basketball", "p:maccabi"),  # legitimate
                ("sharpapi", "event", "milano_b2", "euroleague", "e:game"),
                ("sharpapi", "event", "hapoel_valencia_b3", "euroleague", "e:game"),
                ("football_data", "participant", "4", "football", "p:bvb")):
        db.execute("INSERT INTO provider_identity VALUES(?,?,?,?,?)", row)
    db.execute("INSERT INTO participant_aliases VALUES('basketball','basket','p:milano')")      # inferred, no evidence
    db.execute("INSERT INTO participant_aliases VALUES('football','bvb','p:bvb')")              # curated club code
    db.execute("INSERT INTO picks(pick_date, tier, rank, provider, provider_event_id, sport, league, home, away, "
               "start_utc, market, period, stat, entity, selection, label, price, prob, ev, created_at, status, "
               "fixture_key, canonical_event_id) VALUES('2030-06-01','all',0,'sharpapi','hapoel_valencia_b3',"
               "'basketball','Euroleague','Maccabi Tel Aviv','Valencia Basket Club','2030-06-01T16:00:00Z','handicap',"
               "'full','points','all','home','x',1.95,0.5,0.0,'2030-06-01T08:00:00Z','pending','k1','e:game')")


def test_repair_reports_dry_run_by_default_and_never_touches_records(db, tmp_path):
    _poisoned(db)
    before = {t: count(db, t) for t in ("picks", "canonical_events", "canonical_participants")}
    result = repair(db)
    assert result["dry_run"] and result["would_delete"] == {"provider_identity": 3, "participant_aliases": 1}
    rep = result["report"]
    assert rep["contaminated_events"] == 1 and rep["pending_picks_on_contaminated_events"] == 1
    assert count(db, "provider_identity") == 5 and count(db, "participant_aliases") == 2      # nothing changed
    with pytest.raises(ValueError):
        repair(db, apply=True)                                                                # no backup, no change
    done = repair(db, apply=True, backup_path=str(tmp_path / "backup.db"))
    assert done["deleted"] == {"provider_identity": 3, "participant_aliases": 1}
    assert {t: count(db, t) for t in before} == before                                       # records untouched
    assert count(db, "participant_aliases", "alias='bvb'") == 1                              # curated alias kept
    assert count(db, "provider_identity", "provider_id='name:maccabi tel aviv'") == 1
    backup = sqlite3.connect(str(tmp_path / "backup.db"))
    assert backup.execute("SELECT COUNT(*) FROM provider_identity").fetchone()[0] == 5
    backup.close()
    assert repair(db)["would_delete"] == {"provider_identity": 0, "participant_aliases": 0}  # idempotent


def test_repair_refuses_to_overwrite_an_existing_backup(db, tmp_path):
    _poisoned(db)
    existing = tmp_path / "exists.db"
    existing.write_bytes(b"")
    with pytest.raises(ValueError):
        repair(db, apply=True, backup_path=str(existing))
    assert count(db, "participant_aliases") == 2


def test_audit_on_a_clean_resolution_finds_nothing(db):
    milano, hapoel = _euroleague_night()
    resolve_events_detailed([milano, hapoel], db)
    assert audit(db)["delete"] == {"provider_identity": [], "participant_aliases": []}
