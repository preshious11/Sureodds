"""The stats card, the recap and /yesterday: settled results only, a truthful completeness line for a day that is
not final, hit rate and ROI withheld until it is, and an audit trail that lists every pick with its mark."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from predbot.bot import pagination
from predbot.bot.format import NO_RESULTS_YET, recap_message, stats_message
from predbot.jobs import stats as stats_mod

TODAY = datetime(2030, 6, 2, 9, 30, tzinfo=timezone.utc)      # a Sunday, in the reader's zone (UTC here)
YDAY = "2030-06-01"                                          # Saturday

#: (tier, sport, status, price, prob, label, home, away, hour)
FINAL_DAY = [
    ("top",   "football", "won",  1.69, 0.70, "Over 2.5 Total Goals", "Venezia", "Lazio", 12),
    ("top",   "football", "lost", 1.27, 0.72, "Toulouse (Draw No Bet)", "Toulouse", "Le Havre", 13),
    ("daily", "baseball", "won",  1.54, 0.66, "Cleveland Guardians to Win", "Cleveland Guardians", "Athletics", 14),
    ("daily", "baseball", "lost", 1.30, 0.70, "Boston Red Sox to Win", "Boston Red Sox", "Toronto Blue Jays", 15),
    ("daily", "tennis",   "void", 1.70, 0.60, "Under 2.5 Total Sets", "Iva Jovic", "Peyton Stearns", 16),
    ("daily", "football", "won",  1.69, 0.75, "Over 1.5 Total Goals", "Roma", "Torino", 11),   # ties Venezia on price, higher prob
]
EARLIER = {
    "2030-05-30": [("top", "football", "won", 1.50, 0.7, "Home Win A", "A1", "B1", 12),
                   ("top", "football", "won", 1.40, 0.7, "Home Win B", "A2", "B2", 13),
                   ("daily", "football", "won", 1.80, 0.6, "Home Win C", "A3", "B3", 14),
                   ("daily", "baseball", "lost", 1.90, 0.6, "Home Win D", "A4", "B4", 15),
                   ("daily", "baseball", "void", 1.60, 0.6, "Home Win E", "A5", "B5", 16)],
    "2030-05-31": [("top", "football", "won", 1.45, 0.7, "Home Win F", "A6", "B6", 12),
                   ("top", "football", "lost", 1.55, 0.7, "Home Win G", "A7", "B7", 13),
                   ("daily", "tennis", "won", 2.00, 0.6, "Home Win H", "A8", "B8", 14)],
    "2030-05-29": [("top", "football", "pending", 1.50, 0.7, "Home Win I", "A9", "B9", 12),      # nothing settled that day
                   ("daily", "football", "pending", 1.50, 0.7, "Home Win J", "A10", "B10", 13)],
    "2030-05-20": [("daily", "football", "won", 2.00, 0.6, "Home Win K", "A11", "B11", 12)],      # outside the 7 days, but history
}


def _seed(db, spec_by_day):
    for day, spec in spec_by_day.items():
        for rank, (tier, sport, status, price, prob, label, home, away, hour) in enumerate(spec, start=1):
            pid = db.insert_pick({
                "pick_date": day, "tier": tier, "rank": rank, "provider": "fake", "provider_event_id": f"{day}-{rank}",
                "sport": sport, "league": "L", "home": home, "away": away, "start_utc": f"{day}T{hour:02d}:00:00Z",
                "market": "result3", "period": "full", "stat": "points", "entity": "home", "line": None,
                "selection": "home", "label": label, "price": price, "prob": prob, "ev": 0.05, "p_market": 0.6,
                "sources": {"market": 0.6}})
            if status != "pending":
                db.settle_pick(pid, status)


@pytest.fixture
def final_db(db):
    _seed(db, {**EARLIER, YDAY: FINAL_DAY})
    return db


@pytest.fixture
def partial_db(db):
    partial = [("top", "football", "won", 1.39, 0.7, "Under 3.5 Total Goals", "Toulouse", "Le Havre", 12),
               ("top", "football", "won", 1.30, 0.7, "Over 0.5 Total Goals", "Roma", "Torino", 13),
               ("daily", "baseball", "pending", 1.50, 0.6, "Home Win X", "Angels", "Astros", 14),
               ("daily", "baseball", "pending", 1.50, 0.6, "Home Win Y", "Mets", "Cubs", 15),
               ("daily", "tennis", "pending", 1.50, 0.6, "Home Win Z", "P One", "P Two", 16)]
    _seed(db, {**EARLIER, YDAY: partial})
    return db


# ------------------------------------------------------------------------------------------------ the numbers
def test_final_day_numbers(final_db):
    y = stats_mod.compute(final_db, TODAY)["yesterday"]
    assert (y["issued"], y["won"], y["lost"], y["void"], y["push"], y["settled"], y["final"]) == (6, 3, 2, 1, 0, 6, True)
    assert y["hit_rate"] == pytest.approx(60.0)
    assert y["roi"] == pytest.approx(100 * (0.69 + 0.54 + 0.69 - 2) / 5)         # level stakes; the void is not staked
    assert y["date"] == YDAY


def test_best_win_carries_both_teams_and_ties_break_on_probability(final_db):
    best = stats_mod.compute(final_db, TODAY)["yesterday"]["best_win"]
    assert (best["home"], best["away"], best["price"]) == ("Roma", "Torino", 1.69)      # 1.69 tie: 0.75 beats 0.70
    assert best["outcome"] == "Over 1.5 Total Goals"


def test_best_win_is_none_without_a_win(db):
    _seed(db, {YDAY: [("top", "football", "lost", 1.5, 0.7, "Home Win", "A", "B", 12)]})
    assert stats_mod.compute(db, TODAY)["yesterday"]["best_win"] is None


def test_hit_rate_and_roi_are_withheld_until_the_day_is_final(partial_db):
    y = stats_mod.compute(partial_db, TODAY)["yesterday"]
    assert (y["issued"], y["settled"], y["won"], y["final"]) == (5, 2, 2, False)
    assert y["hit_rate"] is None and y["roi"] is None
    assert y["best_win"]["price"] == 1.39                                          # the record so far still has a best pick


def test_by_sport_yesterday_counts_issued_won_and_settled(final_db):
    rows = stats_mod.compute(final_db, TODAY)["by_sport_yesterday"]
    assert rows == [{"sport": "football", "issued": 3, "won": 2, "settled": 3},
                    {"sport": "baseball", "issued": 2, "won": 1, "settled": 2},
                    {"sport": "tennis", "issued": 1, "won": 0, "settled": 1}]


def test_a_sport_with_nothing_settled_is_still_present(partial_db):
    rows = {r["sport"]: r for r in stats_mod.compute(partial_db, TODAY)["by_sport_yesterday"]}
    assert rows["baseball"] == {"sport": "baseball", "issued": 2, "won": 0, "settled": 0}
    assert rows["football"] == {"sport": "football", "issued": 2, "won": 2, "settled": 2}


def test_last7_counts_settled_only_streak_and_strip(final_db):
    seven = stats_mod.compute(final_db, TODAY)["last7"]
    assert (seven["won"], seven["lost"], seven["void"]) == (8, 4, 2)
    assert seven["hit_rate"] == pytest.approx(100 * 8 / 12)
    assert seven["streak"] == "L1"                                  # chronologically the last decided pick (15:00) is a loss
    strip = [(d["date"], d["marks"]) for d in seven["strip"]]
    assert [d for d, _ in strip] == ["2030-06-01", "2030-05-31", "2030-05-30"]      # most recent first; 05-29 has nothing settled
    assert strip[0][1] == ["won", "lost", "won", "won", "lost", "void"]    # Top legs first, then Daily, each in kickoff order
    assert strip[1][1] == ["won", "lost", "won"]
    assert strip[2][1] == ["won", "won", "won", "lost", "void"]
    assert seven["window_days"] == 7


def test_streaks_of_wins_and_of_losses():
    def rows(*kinds):
        return [{"status": k, "pick_date": "2030-06-01", "start_utc": f"2030-06-01T{10 + i}:00:00Z"} for i, k in enumerate(kinds)]
    assert stats_mod._streak_of(rows("lost", "won", "won", "won")) == "W3"
    assert stats_mod._streak_of(rows("won", "lost", "void", "lost")) == "L2"        # a void neither extends nor breaks it
    assert stats_mod._streak_of(rows("pending")) == ""


def test_top_ticket_counts_only_fully_settled_days(final_db):
    assert stats_mod.compute(final_db, TODAY)["top_ticket_7d"] == {"hit": 1, "days": 3}      # 05-30 hit; 05-31, 06-01 missed; 05-29 pending


def test_top_ticket_that_hit_and_one_that_missed(db):
    _seed(db, {"2030-05-30": [("top", "football", "won", 1.5, .7, "Home Win A", "A", "B", 12),
                              ("top", "football", "void", 1.5, .7, "Home Win B", "C", "D", 13)],       # a void leg drops out
               "2030-05-31": [("top", "football", "won", 1.5, .7, "Home Win C", "E", "F", 12),
                              ("top", "football", "lost", 1.5, .7, "Home Win D", "G", "H", 13)]})
    assert stats_mod.top_ticket([dict(r) for r in db.query("SELECT * FROM picks")]) == {"hit": 1, "days": 2}


# ------------------------------------------------------------------------------------------------ the card
FINAL_CARD = """📊 STATS · Sun 2 Jun

🗓 Yesterday · Sat 1 Jun · 6 picks
✅ 3  ❌ 2  ➖ 1
🎯 60%  ·  💹 ROI -2%
🏆 Best: Over 1.5 Total Goals · Roma vs Torino · @1.69

🏅 By sport
⚽ Football 2/3  ·  ⚾ Baseball 1/2  ·  🎾 Tennis 0/1

📈 Last 7 days
✅ 8  ❌ 4  ➖ 2  ·  🎯 67%  ·  L1
Sat ✅❌✅✅❌➖  Fri ✅❌✅  Thu ✅✅✅❌➖

🎟 Top ticket hit 1 of 3 settled days"""


def test_final_card_text(final_db):
    assert stats_message(stats_mod.compute(final_db, TODAY)) == FINAL_CARD


def test_a_day_that_is_not_final_shows_its_record_so_far_and_one_completeness_line(partial_db):
    text = stats_message(stats_mod.compute(partial_db, TODAY))
    block = text.split("\n\n")[1]
    assert block == ("🗓 Yesterday · Sat 1 Jun · 5 picks\n"
                     "✅ 2  ❌ 0  ➖ 0\n"
                     "2 of 5 settled · final card after the last result\n"
                     "🏆 Best so far: Under 3.5 Total Goals · Toulouse vs Le Havre · @1.39")
    assert "⚽ Football 2/2  ·  ⚾ Baseball —  ·  🎾 Tennis —" in text
    assert "ROI" not in block and "%" not in block and "⏳" not in text and "pending" not in text.lower()
    assert block.count("settled") == 1


def test_fewer_than_seven_days_of_history_says_how_many(db):
    _seed(db, {"2030-05-30": EARLIER["2030-05-30"], "2030-05-31": EARLIER["2030-05-31"]})
    text = stats_message(stats_mod.compute(db, TODAY))
    assert "📈 Last 3 days" in text and "Last 7 days" not in text                 # first pick 05-30 -> 05-30, 05-31, 06-01
    assert "Fri ✅❌✅  Thu ✅✅✅❌➖" in text


def test_empty_state_no_picks_yesterday(db):
    _seed(db, {"2030-05-30": EARLIER["2030-05-30"]})
    text = stats_message(stats_mod.compute(db, TODAY))
    assert "🗓 Yesterday · Sat 1 Jun — no picks were issued." in text
    assert "🏅 By sport" not in text


def test_empty_state_nothing_settled_yet(db):
    _seed(db, {"2030-05-30": EARLIER["2030-05-30"],
               YDAY: [("top", "football", "pending", 1.5, .7, f"Home Win {i}", f"P{i}", f"Q{i}", 12) for i in range(25)]})
    text = stats_message(stats_mod.compute(db, TODAY))
    assert "🗓 Yesterday · Sat 1 Jun · 25 picks — results come in after the last match ends." in text
    assert "✅ 0" not in text.split("📈")[0]


def test_empty_state_no_settled_picks_ever(db):
    assert stats_message(stats_mod.compute(db, TODAY)) == NO_RESULTS_YET == \
        "📊 No results yet — the first recap arrives the morning after the first slate."


@pytest.mark.parametrize("fixture", ["final_db", "partial_db"])
def test_the_card_uses_only_the_allowed_emoji(request, fixture):
    db_ = request.getfixturevalue(fixture)
    allowed = set("📊🗓🏅📈🎟🏆🔥✅❌➖🎯💹⚽🏀🏈⚾🎾🥊") | {chr(0xfe0f), chr(0x21a9)}
    text = stats_message(stats_mod.compute(db_, TODAY))
    assert {c for c in text if ord(c) > 0x2100} <= allowed


# ------------------------------------------------------------------------------------------------ the recap
@pytest.mark.parametrize("fixture", ["final_db", "partial_db"])
def test_recap_renders_through_the_same_helpers_and_the_same_rule(request, fixture):
    db_ = request.getfixturevalue(fixture)
    raw = stats_mod.compute(db_, TODAY)
    stats_body = stats_message(raw).split("\n", 2)[2]
    recap = recap_message(raw)
    assert recap.startswith("📊 RESULTS · Sat 1 Jun\n\n")
    assert recap.split("\n", 2)[2] == stats_body
    assert ("2 of 5 settled · final card after the last result" in recap) == (fixture == "partial_db")


# ------------------------------------------------------------------------------------------------ /yesterday
def _yesterday(db, page=0, size=5):
    return pagination.render_yesterday(db, "UTC", page, size, day=YDAY)


def test_yesterday_lists_every_pick_with_its_mark_in_card_order(final_db):
    p1, _ = _yesterday(final_db, 0)
    p2, _ = _yesterday(final_db, 1)
    assert p1.split("\n")[:2] == ["🗓 YESTERDAY · Sat 1 Jun", "6 picks · ✅ 3 ❌ 2 ➖ 1"]
    assert p1.split("\n\n")[1] == ("🔥 Top\n"
                                   "✅ 🎯 Over 2.5 Total Goals · Venezia vs Lazio · @1.69\n"
                                   "❌ 🎯 Toulouse (Draw No Bet) · Toulouse vs Le Havre · @1.27")
    assert p1.split("\n\n")[2].split("\n") == ["📋 Daily",
                                               "✅ 🎯 Over 1.5 Total Goals · Roma vs Torino · @1.69",       # kickoff 11:00 first
                                               "✅ 🎯 Cleveland Guardians to Win · Cleveland Guardians vs Athletics · @1.54",
                                               "❌ 🎯 Boston Red Sox to Win · Boston Red Sox vs Toronto Blue Jays · @1.30"]
    assert p1.endswith("Page 1 of 2") and p2.endswith("Page 2 of 2")
    assert p2.split("\n")[1] == "6 picks · ✅ 3 ❌ 2 ➖ 1"                    # the header covers the whole day on every page
    assert "➖ 🎯 Under 2.5 Total Sets · Iva Jovic vs Peyton Stearns · @1.70" in p2
    marks = "".join(re.findall(r"^(✅|❌|➖) 🎯", p1 + "\n" + p2, flags=re.M))
    assert marks == "✅❌✅✅❌➖"


@pytest.mark.parametrize("fixture", ["final_db", "partial_db"])
def test_yesterday_header_equals_the_stats_yesterday_numbers_to_the_pick(request, fixture):
    db_ = request.getfixturevalue(fixture)
    y = stats_mod.compute(db_, TODAY)["yesterday"]
    header = _yesterday(db_)[0].split("\n")[1]
    stats_card = stats_message(stats_mod.compute(db_, TODAY))
    assert header == f"{y['issued']} picks · ✅ {y['won']} ❌ {y['lost']} ➖ {y['void']}"
    assert f"✅ {y['won']}  ❌ {y['lost']}  ➖ {y['void']}" in stats_card


def test_yesterday_shows_unsettled_picks_without_a_mark(partial_db):
    page = _yesterday(partial_db, 0)[0]
    assert page.split("\n")[1] == "5 picks · ✅ 2 ❌ 0 ➖ 0"
    assert "\n   🎯 Home Win X · Angels vs Astros · @1.50" in page              # three spaces where the mark would be
    assert "✅ 🎯 Under 3.5 Total Goals · Toulouse vs Le Havre · @1.39" in page
    assert "⏳" not in page


def test_yesterday_with_no_picks(db):
    text, markup = _yesterday(db)
    assert text == "🗓 YESTERDAY · Sat 1 Jun\nNo picks were issued."
    assert markup["inline_keyboard"]


def test_yesterday_page_buttons_carry_the_day_and_the_keyboard_has_a_yesterday_button(final_db):
    _, markup = _yesterday(final_db, 0)
    arrows = [b["callback_data"] for b in markup["inline_keyboard"][0] if b["callback_data"] != "noop"]
    assert arrows == ["pg|yesterday|1|20300601"]
    labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert "🗓 Yesterday" in labels and "📊 Stats" in labels


def test_yesterday_command_and_button_open_the_same_view(final_db, monkeypatch):
    from types import SimpleNamespace
    from predbot.bot import callbacks
    from predbot.bot.callbacks import handle_callback, handle_command
    monkeypatch.setattr(callbacks, "navigation_mode", lambda cfg: cfg.view_navigation)      # the suite pins "edit" by default
    from predbot.cache.models import DailySnapshot
    from predbot.config import Config
    from tests.test_subscribers_broadcast import FakeTG

    service = SimpleNamespace(get_daily_snapshot=lambda: DailySnapshot(date="2030-06-02"))
    tg = FakeTG()
    cfg = Config(timezone="UTC", view_navigation="new")
    assert handle_command("/yesterday", "42", service=service, tg=tg, cfg=cfg, db=final_db)
    handle_callback({"id": "c", "data": "pg|yesterday|0", "message": {"message_id": 1, "chat": {"id": "42"}}},
                    service=service, tg=tg, cfg=cfg, db=final_db)
    assert tg.edits == [] and len(tg.sends) == 2
    assert tg.sends[0]["text"].startswith("🗓 YESTERDAY · ") and tg.sends[0]["reply_markup"] == tg.sends[1]["reply_markup"]
