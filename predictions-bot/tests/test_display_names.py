"""US city-code display aliases: applied at render time only, per sport."""
from __future__ import annotations

import pytest

from predbot.bot.format import pick_line_from_record, pick_lines, recap_message, yesterday_pick_line
from predbot.cache.models import PickRecord
from predbot.util.display_names import CITY_CODES, display_name, display_text
from predbot.util.fixtures import row_fixture_key
from predbot.util.names import canonical_team, same_team


@pytest.mark.parametrize("name,sport,shown", [
    ("BOS Red Sox", "baseball", "Boston Red Sox"),
    ("ARI Diamondbacks", "baseball", "Arizona Diamondbacks"),
    ("TOR Blue Jays", "baseball", "Toronto Blue Jays"),
    ("TEX Rangers", "baseball", "Texas Rangers"),
    ("LA Lakers", "basketball", "Los Angeles Lakers"),
    ("PHO Suns", "basketball", "Phoenix Suns"),
    ("GS Warriors", "basketball", "Golden State Warriors"),
    ("IND Pacers", "basketball", "Indiana Pacers"),
    ("IND Colts", "american_football", "Indianapolis Colts"),          # the same code, per sport
    ("NYG Giants", "american_football", "New York Giants"),
])
def test_codes_become_cities(name, sport, shown):
    assert display_name(name, sport) == shown


@pytest.mark.parametrize("name,sport", [
    ("Manchester City", "football"),
    ("PSV Eindhoven", "football"),
    ("RCD Espanyol", "football"),
    ("LA Galaxy", "football"),                       # football has no table: never touched
    ("DC United", "football"),
    ("Boston Red Sox", "baseball"),                 # already a city name
    ("Athletics", "baseball"),
    ("XYZ Nobody", "baseball"),                     # a code that is not in the table
    ("BOS", "baseball"),                            # no team word after it
    ("bos Red Sox", "baseball"),                    # not all-caps
    ("BOS Red Sox", "tennis"),                      # a sport without a table
    ("", "baseball"),
])
def test_everything_else_is_returned_unchanged(name, sport):
    assert display_name(name, sport) == name


def test_the_table_is_only_for_the_us_leagues_and_every_city_is_a_plain_name():
    assert set(CITY_CODES) == {"baseball", "basketball", "american_football"}
    for table in CITY_CODES.values():
        for code, city in table.items():
            assert code.isupper() and 2 <= len(code) <= 3 and city[0].isupper() and city == city.strip()


def test_display_text_replaces_the_raw_names_inside_an_outcome():
    assert display_text("BOS Red Sox to Win", "TOR Blue Jays", "BOS Red Sox", "baseball") == "Boston Red Sox to Win"
    assert display_text("Over 8.5 Total Runs", "TOR Blue Jays", "BOS Red Sox", "baseball") == "Over 8.5 Total Runs"


def _pick(**kw):
    base = dict(sport="baseball", league="MLB", home="TOR Blue Jays", away="BOS Red Sox", kickoff_utc="2030-06-01T23:10:00Z",
                label="BOS Red Sox to Win", market="result2", selection="away", price=1.6, prob=0.7)
    base.update(kw)
    return PickRecord(**base)


def test_the_pick_block_shows_the_city_names():
    block = pick_lines(pick_line_from_record(_pick(), "UTC"))
    assert block[0] == "⚾ MLB · Toronto Blue Jays vs Boston Red Sox"
    assert block[1] == "🎯 Boston Red Sox to Win"


def test_a_football_block_is_unchanged():
    block = pick_lines(pick_line_from_record(_pick(sport="football", league="MLS", home="LA Galaxy", away="DC United",
                                                   label="LA Galaxy to Win", selection="home"), "UTC"))
    assert block[0] == "⚽ MLS · LA Galaxy vs DC United" and block[1] == "🎯 LA Galaxy to Win"


def test_matching_storage_and_keys_still_use_the_providers_spelling():
    """Only rendering changes: the record, the fixture key and the team matcher keep the raw name."""
    pick = _pick()
    pick_line_from_record(pick, "UTC")
    assert pick.home == "TOR Blue Jays" and pick.label == "BOS Red Sox to Win"
    assert same_team("BOS Red Sox", "Boston Red Sox", sport="baseball")
    row = {"sport": "baseball", "league": "MLB", "home": "TOR Blue Jays", "away": "BOS Red Sox", "start_utc": "2030-06-01T23:10:00Z"}
    swapped = dict(row, home="Toronto Blue Jays", away="Boston Red Sox")
    assert row_fixture_key(row) == row_fixture_key(swapped)          # the key is built from canonical names, untouched by display
    assert canonical_team("BOS Red Sox", "baseball") == canonical_team("Boston Red Sox", "baseball")


def test_yesterday_lines_and_the_recap_best_pick_use_display_names(db):
    row = {"tier": "top", "sport": "baseball", "home": "TOR Blue Jays", "away": "BOS Red Sox", "market": "result2",
           "selection": "away", "label": "BOS Red Sox to Win", "price": 1.6, "period": "full", "stat": "points",
           "entity": "all", "line": None}
    line = yesterday_pick_line(row, "won")
    assert line == "✅ 🎯 Boston Red Sox to Win · Toronto Blue Jays vs Boston Red Sox · @1.60"
    raw = {"yesterday": {"issued": 1, "settled": 1, "won": 1, "lost": 0, "void": 0, "push": 0, "final": True,
                         "hit_rate": 100.0, "roi": 60.0, "date": "2030-06-01",
                         "best_win": {"label": "BOS Red Sox to Win", "outcome": "BOS Red Sox to Win", "home": "TOR Blue Jays",
                                      "away": "BOS Red Sox", "sport": "baseball", "price": 1.6}},
           "last7": {}, "by_sport_yesterday": [], "today": "2030-06-02", "settled_ever": 1}
    text = recap_message(raw)
    assert "🏆 Best: Boston Red Sox to Win · Toronto Blue Jays vs Boston Red Sox · @1.60" in text
    assert "BOS" not in text


def test_the_recap_is_dated_the_day_it_describes():
    raw = {"yesterday": {"issued": 0, "date": "2030-06-01"}, "last7": {}, "by_sport_yesterday": [], "today": "2030-06-02",
           "settled_ever": 3}
    assert recap_message(raw).startswith("📊 RESULTS · Sat 1 Jun")
