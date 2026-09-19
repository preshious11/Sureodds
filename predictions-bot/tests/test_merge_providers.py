"""merge_events: the same match seen through two providers is ONE event - whatever the league-id case, name
spelling or home/away order - with prices pooled and swapped markets re-expressed on the kept event's sides."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from predbot.config import Config
from predbot.markets.settlement import grade
from predbot.providers.base import Event, MarketLine, MatchResult
from predbot.providers.chain import merge_events
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import sc


def _t(hh, mm=0):
    return datetime(2026, 9, 19, hh, mm, tzinfo=timezone.utc)


def _e(provider, home, away, start, league, league_id, sport="baseball", markets=None, extra=None, pid=None):
    return Event(provider=provider, provider_event_id=pid or f"{provider}-{home}", sport=sport, league=league,
                 league_id=league_id, home=home, away=away, start=start, markets=markets or [], sources=[provider],
                 extra=extra or {})


# the four spelled-differently MLB pairs of CROSS_TIER_REPORT.md (SharpAPI names / SGO names), league ids mlb / MLB
MLB_PAIRS = [
    (("Tampa Bay Rays", "BOS Red Sox"), ("Tampa Bay Rays", "Boston Red Sox")),
    (("Texas Rangers", "TOR Blue Jays"), ("Texas Rangers", "Toronto Blue Jays")),
    (("ARI Diamondbacks", "New York Yankees"), ("Arizona Diamondbacks", "New York Yankees")),
    (("Cleveland Guardians", "Oakland Athletics"), ("Cleveland Guardians", "Athletics")),
]


@pytest.mark.parametrize("sharp,sgo", MLB_PAIRS)
def test_the_mlb_pairs_with_different_spellings_and_league_id_case_merge(sharp, sgo):
    a = _e("sharpapi", *sharp, _t(20), "MLB", "mlb")
    b = _e("sportsgameodds", *sgo, _t(20), "MLB", "MLB")
    merged = merge_events([a], [b])
    assert len(merged) == 1 and merged[0].sources == ["sharpapi", "sportsgameodds"]
    assert merged[0].extra["alt_ids"]["sportsgameodds"] == b.provider_event_id
    assert merged[0].extra["merged"][0]["swapped"] is False


def test_a_home_away_swapped_pair_merges():
    a = _e("sharpapi", "TEX Rangers", "BOS Red Sox", _t(18, 10), "MLB", "mlb")
    b = _e("sportsgameodds", "Boston Red Sox", "Texas Rangers", _t(18, 10), "MLB", "MLB")
    (m,) = merge_events([a], [b])
    assert m.home == "TEX Rangers" and m.extra["merged"][0]["swapped"] is True


def test_different_teams_and_different_competitions_do_not_merge():
    lakers = _e("sharpapi", "LA Lakers", "GS Warriors", _t(1), "NBA", "nba", sport="basketball")
    clippers = _e("sportsgameodds", "LA Clippers", "Golden State Warriors", _t(1), "NBA", "NBA", sport="basketball")
    assert len(merge_events([lakers], [clippers])) == 2
    epl = _e("sharpapi", "Leeds United", "Norwich City", _t(15), "England - Premier League", "england_-_premier_league", "football")
    champ = _e("oddsapi", "Leeds United", "Norwich City", _t(15), "Championship", "", "football",
               extra={"sport_key": "soccer_efl_champ"})
    assert len(merge_events([epl], [champ])) == 2


def test_unresolved_competitions_still_need_the_same_league_case_insensitively():
    a = _e("p1", "Team A", "Team B", _t(20), "Some League", "x1", "football")
    assert len(merge_events([a], [_e("p2", "Team A", "Team B", _t(20), "SOME LEAGUE", "X1", "football")])) == 1
    assert len(merge_events([a], [_e("p2", "Team A", "Team B", _t(20), "Other League", "x2", "football")])) == 2


# ------------------------------------------------------------------ swapped markets, graded
def test_a_swapped_providers_handicap_grades_exactly_like_the_same_orientation_provider():
    """Convention (base.MarketLine, settlement._handicap): a handicap's line is applied to the SELECTED side.
    Kept event: home Texas Rangers, away Boston Red Sox. 'Rangers -1.5' is home/-1.5 on the kept event and
    away/-1.5 on the swapped provider's event (its away team is the Rangers); the line keeps its sign."""
    kept_view = MarketLine("handicap", "home", 2.10, line=-1.5, book_prices=[2.10], n_books=1)       # Rangers -1.5
    swapped_view = MarketLine("handicap", "away", 2.30, line=-1.5, book_prices=[2.30], n_books=1)    # Rangers -1.5
    a = _e("sharpapi", "Texas Rangers", "Boston Red Sox", _t(18), "MLB", "mlb", markets=[kept_view])
    b = _e("sportsgameodds", "Boston Red Sox", "Texas Rangers", _t(18), "MLB", "MLB", markets=[swapped_view])
    (m,) = merge_events([a], [b])
    (line,) = [x for x in m.markets if x.market == "handicap"]
    assert (line.selection, line.line) == ("home", -1.5) and sorted(line.book_prices) == [2.10, 2.30]
    for rangers, red_sox, expected in ((5, 4, "lost"), (7, 4, "won"), (5, 5, "lost")):
        kept_result = MatchResult(status="final", scores={"full": (rangers, red_sox)})
        theirs = MatchResult(status="final", scores={"full": (red_sox, rangers)})           # the provider's own orientation
        assert grade(swapped_view, theirs, "baseball") == grade(line, kept_result, "baseball") == \
               grade(kept_view, kept_result, "baseball") == expected
    # +1.5 on the other team: Red Sox +1.5 is away/+1.5 on the kept event and home/+1.5 on the swapped one
    theirs_plus = MarketLine("handicap", "home", 1.7, line=1.5)
    mirrored = theirs_plus.mirrored()
    assert (mirrored.selection, mirrored.line) == ("away", 1.5)
    assert grade(mirrored, MatchResult(status="final", scores={"full": (5, 4)}), "baseball") == \
           grade(theirs_plus, MatchResult(status="final", scores={"full": (4, 5)}), "baseball")


def test_swapped_results_totals_team_totals_and_double_chance_are_re_expressed():
    home_win = MarketLine("result2", "home", 1.9)
    dc = MarketLine("dc", "home_draw", 1.3)
    tt = MarketLine("total", "over", 1.9, line=3.5, entity="home")
    over = MarketLine("total", "over", 1.9, line=8.5)
    assert home_win.mirrored().selection == "away" and dc.mirrored().selection == "away_draw"
    assert tt.mirrored().entity == "away" and tt.mirrored().line == 3.5
    assert over.mirrored() == over


# ------------------------------------------------------------------ pooling
def test_a_merged_market_pools_the_books_and_prices_at_the_median():
    a = _e("sharpapi", "Texas Rangers", "Boston Red Sox", _t(18), "MLB", "mlb",
           markets=[MarketLine("result2", "home", 2.0, book_prices=[2.0, 2.1, 2.2], n_books=3)])
    b = _e("sportsgameodds", "Boston Red Sox", "Texas Rangers", _t(18), "MLB", "MLB",
           markets=[MarketLine("result2", "away", 2.6, book_prices=[2.6, 2.2], n_books=2, provider_odd_id="sgo-id")])
    (m,) = merge_events([a], [b])
    (line,) = m.markets
    assert (line.selection, line.n_books, sorted(line.book_prices)) == ("home", 5, [2.0, 2.1, 2.2, 2.2, 2.6])
    assert line.price == 2.2 and line.provider_odd_id == "sgo-id"          # median of the union; the best price's owner


def test_a_merged_events_top_and_daily_candidates_carry_both_providers():
    a = _e("sharpapi", "Texas Rangers", "Boston Red Sox", dt(hh=22), "MLB", "mlb",
           markets=[MarketLine("result2", "home", 1.45, n_books=2, book_prices=[1.45, 1.45])])
    b = _e("sportsgameodds", "Boston Red Sox", "Texas Rangers", dt(hh=22), "MLB", "MLB",
           markets=[MarketLine("result2", "away", 1.47, n_books=1, book_prices=[1.47])])
    (m,) = merge_events([a], [b])
    assert m.sources == ["sharpapi", "sportsgameodds"]
    s = sc(m, "result2", "home", 0.70, 1.45)
    other = sc(_e("x", "Cubs", "Reds", dt(hh=22), "MLB", "mlb"), "result2", "home", 0.70, 1.45)
    ps = select([s, other], Config(), dt())
    assert s in ps.top and set(s.event.sources) == {"sharpapi", "sportsgameodds"}


def test_the_similarity_floor_shortcut_never_changes_a_same_team_answer():
    import itertools
    from difflib import SequenceMatcher
    from predbot.util.names import normalize, same_team, similarity
    names = ["Arsenal", "Arsenal FC", "Chelsea", "Real Madrid", "Atletico Madrid", "Barcelona", "Barcelona B", "Espanyol",
             "RCD Espanyol", "Manchester United", "Manchester City", "Leeds United", "Norwich City", "Le Mans", "Lorient",
             "Home1", "Home2", "Bayern Munich", "FC Bayern", "Sporting CP", "Sporting Lisbon", "Rangers", "Texas Rangers"]
    for a, b in itertools.product(names, names):
        assert (similarity(a, b) >= 0.82) == same_team(a, b), (a, b)
