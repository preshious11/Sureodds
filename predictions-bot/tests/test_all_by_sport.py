"""All Predictions: a sport menu first (only sports with predictions today), then that sport's pages.

One stored pool; the menu and the sport pages are views over it and never reach a provider."""
from __future__ import annotations

from types import SimpleNamespace

from predbot.bot import pagination
from predbot.bot.callbacks import handle_callback, handle_command
from predbot.bot.format import SPORT_LABEL
from predbot.cache.models import KNOWN_SPORTS, DailySnapshot, PickRecord
from predbot.config import Config
from tests.test_subscribers_broadcast import FakeTG

DAY = "2030-06-01"


def _pick(i, sport):
    return PickRecord(pick_id=i, tier="all", rank=i, sport=sport, league="L", home=f"{sport}Home{i}",
                      away=f"{sport}Away{i}", kickoff_utc=f"2030-06-01T{10 + i % 10:02d}:00:00Z",
                      label="Home to Win", market="result2", selection="home", price=1.6, prob=0.7,
                      pick_date=DAY)


def _snapshot():
    picks = [_pick(i, "football") for i in range(7)] + [_pick(20 + i, "tennis") for i in range(2)] \
        + [_pick(30, "baseball")]
    return DailySnapshot(date=DAY, origin="db", all_predictions=picks)


def _buttons(markup):
    return [b for row in markup["inline_keyboard"] for b in row]


def test_the_menu_lists_only_sports_with_predictions_and_their_counts():
    text, markup = pagination.render_view(_snapshot(), "all", 0, 5, "UTC")
    assert "ALL PREDICTIONS" in text and "10 predictions · 10 matches" in text and "Pick a sport" in text
    labels = [b["text"] for b in _buttons(markup) if b["callback_data"].startswith("pg|all_")]
    assert labels == ["⚽ Football (7 · 7 matches)", "🎾 Tennis (2 · 2 matches)", "⚾ Baseball (1 · 1 match)"]     # no basketball: none today
    assert "Home" not in text                                                  # no prediction cards on the menu


def test_a_sport_view_shows_only_that_sports_predictions_with_pagination_inside_the_sport():
    snap = _snapshot()
    first, markup = pagination.render_view(snap, "all_football", 0, 5, "UTC")
    assert "Football" in first and "7 predictions · 7 matches" in first and "Page 1 of 2" in first
    assert first.count("footballHome") == 5 and "tennisHome" not in first and "baseballHome" not in first
    data = [b["callback_data"] for b in _buttons(markup)]
    assert "pg|all_football|1|20300601" in data                                  # next page stays in the sport
    assert "pg|all|0|20300601" in data                                           # back to the sport menu
    second, markup2 = pagination.render_view(snap, "all_football", 1, 5, "UTC", day=DAY)
    assert second.count("footballHome") == 2 and "Page 2 of 2" in second
    assert "pg|all_football|0|20300601" in [b["callback_data"] for b in _buttons(markup2)]


def test_every_menu_and_sport_callback_round_trips():
    _, markup = pagination.render_view(_snapshot(), "all", 0, 5, "UTC")
    for b in _buttons(markup):
        if b["callback_data"].startswith("pg|all_"):
            view, page, day = pagination.parse_callback(b["callback_data"])
            assert view.startswith("all_") and page == 0 and day == DAY


def test_a_sport_with_nothing_today_and_unknown_sports_are_handled():
    text, markup = pagination.render_view(_snapshot(), "all_basketball", 0, 5, "UTC")
    assert text == "No Basketball predictions today."
    assert "pg|all|0|20300601" in [b["callback_data"] for b in _buttons(markup)]
    snap = DailySnapshot(date=DAY, all_predictions=[_pick(1, "cricket")])
    menu, markup = pagination.render_view(snap, "all", 0, 5, "UTC")
    assert any(b["text"] == "🎯 Other (1 · 1 match)" for b in _buttons(markup))
    assert "cricketHome1" in pagination.render_view(snap, "all_other", 0, 5, "UTC")[0]


def test_known_sports_match_the_display_labels():
    assert set(KNOWN_SPORTS) == set(SPORT_LABEL) - {"other"}


def test_command_menu_and_sport_taps_make_no_provider_call_and_page_in_place():
    snap = _snapshot()
    calls = []

    def get(allow_api_call=True, **_):
        calls.append(allow_api_call)
        if allow_api_call and len(calls) > 1:
            raise AssertionError("a sport tap or page turn asked for a generation")
        return snap

    service = SimpleNamespace(get_daily_snapshot=get, snapshot_for=lambda d: snap)
    tg, cfg = FakeTG(), Config(timezone="UTC")
    handle_command("/all", "42", service=service, tg=tg, cfg=cfg, db=None)
    assert "Pick a sport" in tg.sends[0]["text"]
    for data in ("pg|all_football|0|20300601", "pg|all_football|1|20300601", "pg|all|0|20300601"):
        handle_callback({"id": "c", "data": data, "message": {"message_id": 9, "chat": {"id": "42"}}},
                        service=service, tg=tg, cfg=cfg, db=None)
    assert [e["message_id"] for e in tg.edits] == [9, 9, 9]                      # drill-down edits in place
    assert "footballHome" in tg.edits[0]["text"] and "tennisHome" not in tg.edits[0]["text"]
    assert "Pick a sport" in tg.edits[2]["text"]
    assert calls[1:] == [False, False, False] or all(c is False for c in calls[1:])


# ---------------------------------------------------------------------- several predictions per fixture
_LINE = {"result3": ("home", None, "Angers to Win"), "dc": ("home_draw", None, "Angers or Troyes"), "dnb": ("home", None, "Angers (DNB)"),
         "handicap": ("home", -1.5, "Angers -1.5"), "total": ("over", 2.5, "Over 2.5 Goals"), "btts": ("yes", None, "BTTS Yes")}


def _fixture_picks(fixture, n_from, markets, hour=19):
    out = []
    for k, market in enumerate(markets):
        selection, line, label = _LINE[market]
        out.append(PickRecord(pick_id=n_from + k, tier="all", rank=n_from + k, sport="football", league="Ligue 1", home=f"Home{fixture}",
                              away=f"Away{fixture}", kickoff_utc=f"2030-06-01T{hour:02d}:45:00Z", label=label, market=market,
                              selection=selection, line=line, price=1.5 + k / 10, prob=0.6 + k / 100, pick_date=DAY, fixture_id=f"fx-{fixture}"))
    return out


def test_predictions_of_one_fixture_share_one_header_in_the_stable_family_order():
    scrambled = ["btts", "total", "result3", "dnb", "handicap", "dc"]
    snap = DailySnapshot(date=DAY, origin="db", all_predictions=_fixture_picks(1, 1, scrambled))
    text, _ = pagination.render_view(snap, "all_football", 0, 5, "UTC")
    assert text.count("Home1 vs Away1") == 1                                     # one header for the fixture
    assert "6 predictions · 1 match" in text
    order = [text.index(x) for x in ("to Win", "Troyes", "(DNB)", "-1.5", "Over 2.5", "BTTS")]
    assert order == sorted(order)                                                # 1X2, DC, DNB, Handicap, Over/Under, BTTS
    assert text.count("🎯") == 6 and "💰" in text and "📊" in text
    assert "rule" not in text.lower() and "percentile" not in text.lower()      # no technical model information


def test_a_fixture_is_never_split_across_pages_and_pages_count_fixtures():
    picks = []
    for f in range(6):
        picks += _fixture_picks(f, 100 + f * 10, ["result3", "dc", "total", "btts"], hour=10 + f)
    snap = DailySnapshot(date=DAY, origin="db", all_predictions=picks)
    first, markup = pagination.render_view(snap, "all_football", 0, 5, "UTC")
    second, _ = pagination.render_view(snap, "all_football", 1, 5, "UTC", day=DAY)
    assert "Page 1 of 2" in first and "24 predictions · 6 matches" in first
    assert all(f"Home{f} vs Away{f}" in first and first.count(f"Home{f} vs") == 1 for f in range(5))   # 5 fixtures, 20 lines
    assert first.count("🎯") == 20 and second.count("🎯") == 4 and "Home5 vs Away5" in second
    for f in range(6):
        page = first if f < 5 else second
        assert page.count(f"Away{f}") == 1                                       # the header appears once, on one page only


def test_the_menu_counts_predictions_and_matches_separately():
    snap = DailySnapshot(date=DAY, origin="db", all_predictions=_fixture_picks(1, 1, ["result3", "total"]) + _fixture_picks(2, 5, ["result3"]))
    text, markup = pagination.render_view(snap, "all", 0, 5, "UTC")
    assert "3 predictions · 2 matches" in text
    assert [b["text"] for b in _buttons(markup) if b["callback_data"].startswith("pg|all_")] == ["⚽ Football (3 · 2 matches)"]
