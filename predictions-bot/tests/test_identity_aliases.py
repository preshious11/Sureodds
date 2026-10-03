"""Provider spellings of one participant resolve to ONE canonical participant and event, learned through the
canonical identity registry (no fixture-specific rule)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from predbot.providers.base import Event, MarketLine
from predbot.storage.identity import IdentityRegistry, resolve_events, unique_failed_fixtures, failure_record

KICK = datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc)
LEAGUE = "England - Premier League"


def _ev(provider, home, away, start=KICK, league=LEAGUE, eid=None):
    return Event(provider=provider, provider_event_id=eid or f"{provider}:{home}:{away}", sport="football",
                 league=league, home=home, away=away, start=start, markets=[MarketLine("result3", "home", 2.0)])


def test_estac_troyes_and_troyes_resolve_to_one_canonical_event(db):
    a = _ev("sharpapi", "Angers", "Troyes")
    b = _ev("sportsgameodds", "Angers", "ESTAC Troyes", start=KICK + timedelta(minutes=10))
    kept, failures = resolve_events([a, b], db)
    assert not failures and len(kept) == 2
    assert a.canonical_event_id == b.canonical_event_id
    assert (a.home_id, a.away_id) == (b.home_id, b.away_id)
    assert db.query("SELECT COUNT(*) AS n FROM canonical_events")[0]["n"] == 1
    assert db.query("SELECT COUNT(*) AS n FROM canonical_participants")[0]["n"] == 2
    # the alias is persisted: a third provider using the same spelling needs no anchor at all
    c = _ev("oddsapi", "Angers", "ESTAC Troyes", start=KICK + timedelta(hours=40))
    resolve_events([c], db)
    assert c.away_id == a.away_id and c.home_id == a.home_id
    assert db.query("SELECT canonical_id FROM participant_aliases WHERE alias='estac troyes'")[0]["canonical_id"] == a.away_id


def test_resolution_is_independent_of_the_order_providers_arrive_in(db):
    a = _ev("sportsgameodds", "Angers", "ESTAC Troyes")
    b = _ev("sharpapi", "Angers", "Troyes")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id


def test_the_anchor_can_be_the_away_side(db):
    a = _ev("sharpapi", "Lyon FC", "Rennes")
    b = _ev("sportsgameodds", "Olympique Lyonnais", "Rennes")
    resolve_events([a, b], db)
    assert a.canonical_event_id == b.canonical_event_id


def test_different_fixtures_are_never_merged(db):
    same_time = [_ev("sharpapi", "Angers", "Troyes"), _ev("sharpapi", "Nantes", "Lorient"),
                 _ev("sportsgameodds", "Nantes", "Lorient")]
    later = _ev("sharpapi", "Angers", "Nantes", start=KICK + timedelta(days=7))           # same club, a week later
    other_league = _ev("sharpapi", "Angers", "Brest", league="Spain - La Liga")
    resolve_events(same_time + [later, other_league], db)
    ids = {e.canonical_event_id for e in same_time}
    assert len(ids) == 2                                       # Angers/Troyes and Nantes/Lorient (two providers)
    assert later.canonical_event_id not in ids and other_league.canonical_event_id not in ids
    assert later.canonical_event_id != other_league.canonical_event_id


def test_an_unanchored_unknown_pair_stays_separate_rather_than_guessed(db):
    a = _ev("sharpapi", "Angers", "Troyes")
    b = _ev("sportsgameodds", "Metz", "ESTAC Troyes")        # neither side anchors to a fixture with the same side
    resolve_events([a, b], db)
    assert a.canonical_event_id != b.canonical_event_id


def test_a_fixture_failing_through_two_providers_is_still_counted_once(db):
    bad_a, bad_b = _ev("sharpapi", "Nowhere", "Nowhere"), _ev("sportsgameodds", "Nowhere", "Nowhere")
    _, failed = __import__("predbot.storage.identity", fromlist=["x"]).resolve_events_detailed([bad_a, bad_b], db)
    records = [failure_record(e, r) for e, r in failed]
    assert len(records) == 2 and len(unique_failed_fixtures(records, [])) == 1


def _college(provider, home, away, start=KICK):
    return Event(provider=provider, provider_event_id=f"{provider}:{home}:{away}", sport="american_football", league="NCAAF",
                 league_id="NCAAF", home=home, away=away, start=start, markets=[MarketLine("result2", "home", 1.5)])


def test_two_state_schools_are_distinct_participants_not_one_team_called_state(db):
    """Nickname reduction used to turn 'Ohio State' and 'Kent State' into the same participant ('state')."""
    a, b = _college("sportsgameodds", "Ohio State", "Kent State"), _college("sportsgameodds", "Michigan State", "Penn State")
    kept, failures = resolve_events([a, b], db)
    assert not failures and len(kept) == 2
    assert len({a.home_id, a.away_id, b.home_id, b.away_id}) == 4
    assert db.query("SELECT COUNT(*) AS n FROM canonical_participants WHERE sport='american_football'")[0]["n"] == 4


def test_a_college_name_with_and_without_its_nickname_is_one_participant(db):
    a = _college("sharpapi", "Ohio State", "Kent State")
    b = _college("sportsgameodds", "Ohio State Buckeyes", "Kent State Golden Flashes", start=KICK + timedelta(minutes=5))
    resolve_events([a, b], db)
    assert (a.home_id, a.away_id) == (b.home_id, b.away_id) and a.canonical_event_id == b.canonical_event_id


def test_professional_nickname_reduction_is_unchanged(db):
    from predbot.util.names import canonical_team
    assert canonical_team("LA Dodgers", "baseball") == canonical_team("Los Angeles Dodgers", "baseball") == "dodgers"
    assert canonical_team("Chicago White Sox", "baseball") == "white sox"
    assert canonical_team("Ohio State", "american_football") == "ohio state"
