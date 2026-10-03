"""The bot's established Telegram look, pinned.

The architecture work (one stored pool, several predictions per fixture, /all by sport) must not change the emojis, the
separators or the card layout readers already know. Every string below was the production format BEFORE the refactor
(commit 4e7bbd7); /all only re-uses it, grouped by fixture. Change one of these on purpose and you change the product."""
from __future__ import annotations

from datetime import datetime, timezone

from predbot.bot import format as fmt
from predbot.bot import pagination
from predbot.cache.models import DailySnapshot, PickRecord
from predbot.storage.db import Database
from tests.test_all_predictions_architecture import row

DAY = "2030-06-01"


def _record(i, market="result3", selection="home", label="Angers to Win", line=None, price=2.02, prob=0.46, fixture="fx-1", hour=19):
    return PickRecord(pick_id=i, tier="all", rank=i, sport="football", league="Ligue 1", country="France", home="Angers",
                      away="ESTAC Troyes", kickoff_utc=f"2030-06-01T{hour:02d}:45:00Z", label=label, market=market,
                      selection=selection, line=line, price=price, prob=prob, pick_date=DAY, fixture_id=fixture)


# --------------------------------------------------------------------------------------------------- the vocabulary
def test_the_emoji_vocabulary_is_frozen():
    named = ("top", "daily", "stats", "yesterday", "all")
    assert {v: pagination.VIEW_ICON[v] for v in named} == {"top": "🔥", "daily": "📋", "stats": "📊", "yesterday": "🗓", "all": "📚"}
    assert (fmt.TOP_TITLE, fmt.DAILY_TITLE) == ("🔥 TOP PICKS", "📋 DAILY PICKS")
    assert fmt.MARK == {"won": "✅", "lost": "❌", "void": "➖", "push": "↩️"}
    assert (fmt.STARTED_MARK, fmt.FINISHED_MARK) == (" 🔴", " · Finished")
    assert [fmt.SPORT_ICON[s] for s in ("football", "basketball", "baseball", "tennis")] == ["⚽", "🏀", "⚾", "🎾"]
    assert [pagination.view_button_label(v) for v in ("top", "daily", "stats", "yesterday", "all")] == \
        ["🔥 Top Picks", "📋 Daily Picks", "📊 Stats", "🗓 Yesterday", "📚 All Predictions"]


def test_a_pick_block_is_three_lines_with_the_original_separators():
    p = pagination.pick_line_from_record(_record(1), "UTC")
    assert fmt.pick_lines(p) == ["⚽ France • Ligue 1 · Angers vs ESTAC Troyes", "🎯 Angers to Win",
                                 "💰 2.02  ·  📊 46%  ·  🕘 7:45 PM"]


def test_the_top_and_daily_card_headers_and_blocks_are_unchanged():
    picks = [pagination.pick_line_from_record(_record(i, hour=18 + i), "UTC") for i in (1, 2)]
    card = fmt.picks_card(picks, DAY, title=fmt.TOP_TITLE, zone="UTC")
    assert card.splitlines()[:2] == ["🔥 TOP PICKS · Sat 1 Jun", "🎟 Combined odds 4.08 · 2 picks · 🕘 times in UTC"]
    assert card.splitlines()[2:6] == ["", "⚽ France • Ligue 1 · Angers vs ESTAC Troyes", "🎯 Angers to Win",
                                      "💰 2.02  ·  📊 46%  ·  🕘 7:45 PM"]
    assert fmt.picks_card(picks, DAY, title=fmt.DAILY_TITLE, zone="UTC").startswith("📋 DAILY PICKS · Sat 1 Jun\n🎟 Combined odds")


# --------------------------------------------------------------------------------------------------- /all
def test_all_reuses_the_original_pick_lines_grouped_under_one_header_per_fixture():
    picks = [_record(1), _record(2, "dc", "home_draw", "Angers or ESTAC Troyes", price=1.31, prob=0.72),
             _record(3, "btts", "yes", "BTTS Yes", price=1.62, prob=0.59)]
    snap = DailySnapshot(date=DAY, origin="db", all_predictions=picks)
    text, _ = pagination.render_view(snap, "all_football", 0, 5, "UTC")
    assert text == "\n".join([
        "📚 ALL PREDICTIONS · Sat 1 Jun",
        "⚽ Football · 3 predictions · 1 match · 🕘 times in UTC",
        "",
        "⚽ France • Ligue 1 · Angers vs ESTAC Troyes",
        "🎯 Angers to Win",
        "💰 2.02  ·  📊 46%  ·  🕘 7:45 PM",
        "🎯 Angers or ESTAC Troyes",
        "💰 1.31  ·  📊 72%  ·  🕘 7:45 PM",
        "🎯 BTTS Yes",
        "💰 1.62  ·  📊 59%  ·  🕘 7:45 PM"])                                          # single page: no footer, like Top / Daily
    # each prediction's two lines are exactly the lines the Top / Daily card prints for it
    for p in picks:
        original = fmt.pick_lines(pagination.pick_line_from_record(p, "UTC"))
        assert "\n".join(original[1:]) in text and original[0] in text


def test_all_menu_and_buttons_follow_the_established_style():
    snap = DailySnapshot(date=DAY, origin="db", all_predictions=[_record(1), _record(2, "btts", "yes", "BTTS Yes", fixture="fx-2")])
    text, markup = pagination.render_view(snap, "all", 0, 5, "UTC")
    assert text == "📚 ALL PREDICTIONS · Sat 1 Jun\n2 predictions · 2 matches\n\nPick a sport:"
    rows = [[b["text"] for b in r] for r in markup["inline_keyboard"]]
    assert rows == [["⚽ Football (2 · 2 matches)"], ["📚 All Predictions"], ["🔥 Top Picks", "📋 Daily Picks"], ["📊 Stats", "🗓 Yesterday"]]


def test_all_pages_use_the_original_footer_and_keep_the_sports_back_button():
    picks = [p for f in range(6) for p in (_record(10 * f + 1, fixture=f"fx-{f}", hour=10 + f),)]
    for p, f in zip(picks, range(6)):
        p.home = f"Home{f}"
    snap = DailySnapshot(date=DAY, origin="db", all_predictions=picks)
    text, markup = pagination.render_view(snap, "all_football", 0, 5, "UTC")
    assert text.endswith("\n\nPage 1 of 2")
    texts = [b["text"] for r in markup["inline_keyboard"] for b in r]
    assert "◀️ Sports" in texts and "▶️" in texts and texts.index("▶️") < texts.index("◀️ Sports")


# --------------------------------------------------------------------------------------------------- stats
def _settled_db(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    for k, status in enumerate(("won", "lost", "won")):
        pid = db.store_prediction(row(f"e{k}", pick_date="2030-05-31", market="result3" if k < 2 else "total",
                                      selection="home" if k < 2 else "over", line=None if k < 2 else 2.5))
        db.settle_pick(pid, status)
        db.add_membership(pid, "top", k + 1)                                            # published, so the /stats card counts it
    db.store_prediction(row("e9", pick_date=DAY))                                    # one pending, today
    return db


def test_the_all_predictions_stats_card_is_worded_like_the_stats_card(tmp_path):
    db = _settled_db(tmp_path)
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
    text, markup = pagination.render_view(DailySnapshot(date=DAY), "stats_all", 0, 5, "UTC", db=db, now=now)
    lines = text.splitlines()
    assert lines[0] == "📊 STATS · 📚 All Predictions · Sat 1 Jun"
    assert "🗓 Today" in lines and "📈 Last 7 days" in lines and "📈 All time" in lines
    assert "✅ 2  ❌ 1  ➖ 0  ·  🎯 67%" in lines                                       # the counts_line / hit-rate wording
    assert "4 predictions · 4 matches" in lines and "⏳ 1 pending" in lines
    assert "Performance" not in text and "<b>" not in text
    buttons = [b["text"] for r in markup["inline_keyboard"] for b in r]
    assert buttons[:1] == ["📊 View Results"] and "🔥 Top stats" in buttons and "📋 Daily stats" in buttons and "📚 All stats" in buttons
    results, _ = pagination.render_view(DailySnapshot(date=DAY), "results_all", 0, 5, "UTC", db=db, now=now)
    assert results.splitlines()[0] == "🗓 ALL PREDICTIONS · RESULTS" and "✅ 🎯" in results


def test_the_main_stats_card_is_the_original_one_and_gains_only_a_button_row(tmp_path):
    db = _settled_db(tmp_path)
    now = datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc)
    text, markup = pagination.render_stats(DailySnapshot(date=DAY), db=db, tz="UTC", now=now)
    assert text.startswith("📊 STATS · Sat 1 Jun") and "Performance" not in text
    rows = [[b["text"] for b in r] for r in markup["inline_keyboard"]]
    assert rows[1:] == [["📚 All Predictions"], ["🔥 Top Picks", "📋 Daily Picks"], ["📊 Stats", "🗓 Yesterday"]]   # the usual switch rows
    assert rows[0] == ["🔥 Top stats", "📋 Daily stats", "📚 All stats"]


def test_yesterday_keeps_its_original_lines():
    line = fmt.yesterday_pick_line({"sport": "football", "home": "Angers", "away": "ESTAC Troyes", "price": 1.31, "market": "dc",
                                    "selection": "home_draw", "line": None, "label": "Angers or ESTAC Troyes", "period": "full",
                                    "stat": "points", "entity": "all"}, "won")
    assert line == "✅ 🎯 Angers or ESTAC Troyes · Angers vs ESTAC Troyes · @1.31"
