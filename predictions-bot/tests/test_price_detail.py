"""prediction_row() records who supplied the published price and every book's quote, additively."""
import json

from predbot.jobs.daily_picks import prediction_row
from predbot.providers.base import MarketLine
from tests.test_picker import ev, sc


def _row(**ml_kw):
    s = sc(ev(1), "result3", "home", 0.7, 1.65)
    for k, v in ml_kw.items():
        setattr(s.line, k, v)
    return prediction_row(s, "2026-10-23")


def test_book_quotes_and_price_source_are_stored_in_notes():
    row = _row(bookmaker="median", book_quotes={"bet365": 1.62, "williamhill": 1.65, "pinnacle": 1.74})
    notes = row["sources"]["_notes"]
    assert notes["price_source"] == "median"
    assert notes["book_quotes"] == {"bet365": 1.62, "pinnacle": 1.74, "williamhill": 1.65}


def test_the_published_price_and_every_other_field_are_unchanged():
    s = sc(ev(1), "result3", "home", 0.7, 1.65)
    s.line.book_quotes = {"bet365": 1.62, "pinnacle": 1.74}
    row = prediction_row(s, "2026-10-23")
    assert row["price"] == 1.65                      # still the line's own price, never the best quote
    assert row["sources"]["_notes"]["edge"] == s.edge
    for key, value in s.notes.items():
        assert row["sources"]["_notes"][key] == value


def test_a_line_without_per_book_quotes_stores_an_empty_map_and_stays_json_safe():
    row = _row(bookmaker="", book_quotes={})
    notes = row["sources"]["_notes"]
    assert notes["price_source"] is None
    assert notes["book_quotes"] == {}
    json.dumps(row["sources"], allow_nan=False)


def test_invalid_quotes_are_dropped():
    row = _row(bookmaker="bet365", book_quotes={"bet365": 1.6, "broken": 0, "evens": 1.0})
    assert row["sources"]["_notes"]["book_quotes"] == {"bet365": 1.6}
