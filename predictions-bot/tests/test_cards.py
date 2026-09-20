"""The picks cards (Top, Daily and its pages, the delivered card, the channel post, empty states): golden text from
a fixed snapshot, kickoff order, each card's OWN odds and count across every page, the reader's zone in the header,
the emoji vocabulary, and delivery that never cuts a pick block or an HTML tag."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from predbot.bot import broadcast, pagination
from predbot.bot.format import (DAILY_TITLE, NO_SELECTION, TOP_TITLE, picks_card, pick_line_from_record,
                                summary_card, zone_label)
from predbot.bot.telegram import MAX_LEN, Telegram, _split
from predbot.cache.models import DailySnapshot, PickRecord

NOW = datetime(2030, 6, 1, 6, 0, tzinfo=timezone.utc)          # before every kickoff below
DAY = "2030-06-01"
LAGOS = "Africa/Lagos"


def rec(i, tier, hour, price=1.25, prob=0.70, league="MLS", country="", home=None, away=None, label=None):
    """A pick kicking off ``hour`` hours after 2030-06-01 00:00Z."""
    kickoff = (datetime(2030, 6, 1, tzinfo=timezone.utc) + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return PickRecord(pick_id=i, tier=tier, rank=i, sport="football", league=league, country=country,
                      home=home or f"Home {i}", away=away or f"Away {i}", label=label or f"Home {i} to Win",
                      market="result2", selection="home", kickoff_utc=kickoff, price=price, prob=prob)


def snapshot(daily=12):
    # slate order is deliberately NOT kickoff order: the cards sort at render time
    top = [rec(1, "top", 23, 1.31, 0.76, home="DC United", away="Charlotte FC", label="DC United or Charlotte FC"),
           rec(2, "top", 16, 1.53, 0.64, league="Premier League", country="England", home="Chelsea", away="Arsenal",
               label="Over 2.5 Total Goals")]
    return DailySnapshot(date=DAY, top_picks=top, daily_picks=[rec(10 + i, "daily", 22 - i) for i in range(daily)])


def nl(*lines):
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ golden text
def test_top_card():
    text, markup = pagination.render_page(snapshot(), "top", 0, 5, LAGOS, now=NOW)
    assert text == nl(
        "🔥 TOP PICKS · Sat 1 Jun",
        "🎟 Combined odds 2.00 · 2 picks · 🕘 times in WAT",
        "",
        "⚽ England • Premier League · Chelsea vs Arsenal",
        "🎯 Over 2.5 Total Goals",
        "💰 1.53  ·  📊 64%  ·  🕘 5:00 PM",
        "",
        "⚽ MLS · DC United vs Charlotte FC",
        "🎯 DC United or Charlotte FC",
        "💰 1.31  ·  📊 76%  ·  🕘 12:00 AM")
    assert [b["callback_data"] for row in markup["inline_keyboard"] for b in row] == \
        ["pg|top|0", "pg|daily|0", "pg|stats|0", "pg|yesterday|0"]


def test_daily_page_2_of_3():
    text, markup = pagination.render_page(snapshot(), "daily", 1, 5, LAGOS, now=NOW)
    assert text == nl(
        "📋 DAILY PICKS · Sat 1 Jun",
        f"🎟 Combined odds {1.25 ** 12:.2f} · 12 picks · 🕘 times in WAT",
        "",
        "⚽ MLS · Home 16 vs Away 16", "🎯 Home 16 to Win", "💰 1.25  ·  📊 70%  ·  🕘 5:00 PM", "",
        "⚽ MLS · Home 15 vs Away 15", "🎯 Home 15 to Win", "💰 1.25  ·  📊 70%  ·  🕘 6:00 PM", "",
        "⚽ MLS · Home 14 vs Away 14", "🎯 Home 14 to Win", "💰 1.25  ·  📊 70%  ·  🕘 7:00 PM", "",
        "⚽ MLS · Home 13 vs Away 13", "🎯 Home 13 to Win", "💰 1.25  ·  📊 70%  ·  🕘 8:00 PM", "",
        "⚽ MLS · Home 12 vs Away 12", "🎯 Home 12 to Win", "💰 1.25  ·  📊 70%  ·  🕘 9:00 PM", "",
        "Page 2 of 3")
    assert [b["text"] for b in markup["inline_keyboard"][0]] == ["◀️", "2/3", "▶️"]


def test_delivered_card_has_both_tickets_each_with_its_own_numbers():
    text = broadcast.picks_text(snapshot(), "Europe/London", now=NOW)
    top, daily = text.split("\n\n" + DAILY_TITLE)
    assert top.startswith(nl(TOP_TITLE + " · Sat 1 Jun", "🎟 Combined odds 2.00 · 2 picks · 🕘 times in BST"))
    assert daily.startswith(f" · Sat 1 Jun\n🎟 Combined odds {1.25 ** 12:.2f} · 12 picks · 🕘 times in BST")
    assert top.count("💰") == 2 and daily.count("💰") == 12
    assert "Generated" not in text and "odds as of" not in text and "TODAY'S RESULTS" not in text


def test_empty_states_keep_their_wording_and_gain_the_lead_emoji():
    def view(generation, status="COMPLETE", view_="top", **kw):
        snap = DailySnapshot(date=DAY, status=status, generation=generation, **kw)
        return pagination.render_page(snap, view_, 0, 5, LAGOS)[0]

    assert view({"reason": "before_post_time", "ready_at": "09:00 WAT"}) == "📋 Today's picks are ready at 09:00 WAT."
    assert view({"reason": "generating"}) == "📋 Today's picks are being generated — try again in a minute."
    assert view({}) == nl("🔥 TOP PICKS · Sat 1 Jun", NO_SELECTION)
    assert view({}, view_="daily") == nl("📋 DAILY PICKS · Sat 1 Jun", NO_SELECTION)
    assert view({}, "PARTIAL").startswith("📋 <b>Today's predictions are still being assembled</b>")


# ------------------------------------------------------------------------------------------------ the rules
def test_picks_are_in_kickoff_order_on_every_card():
    snap = snapshot()
    hours = lambda text: [int(h) % 12 or 12 for h in re.findall(r"🕘 (\d+):00", text)]   # noqa: E731
    top, _ = pagination.render_page(snap, "top", 0, 5, "UTC", now=NOW)
    assert top.index("Chelsea") < top.index("DC United")                            # 16:00Z before 23:00Z
    page1, _ = pagination.render_page(snap, "daily", 0, 5, "UTC", now=NOW)
    page3, _ = pagination.render_page(snap, "daily", 2, 5, "UTC", now=NOW)
    assert hours(page1) == [11, 12, 1, 2, 3] and hours(page3) == [9, 10]              # 11:00..22:00 UTC, across pages
    assert [p.pick_id for p in snap.daily_picks][:2] == [10, 11]                    # the slate itself is untouched
    delivered = broadcast.picks_text(snap, "UTC", now=NOW)
    assert delivered.index("Chelsea") < delivered.index("DC United")
    assert delivered.index("Home 21 vs") < delivered.index("Home 11 vs")


@pytest.mark.parametrize("page,expected_count", [(0, 5), (1, 5), (2, 2)])
def test_every_page_carries_the_whole_tickets_odds_and_count(page, expected_count):
    snap = snapshot(daily=12)
    snap.daily_picks = snap.daily_picks[:12]
    text, _ = pagination.render_page(snap, "daily", page, 5, LAGOS, now=NOW)
    assert f"🎟 Combined odds {math.prod(p.price for p in snap.daily_picks):.2f} · 12 picks" in text
    assert text.count("💰") == expected_count                                        # only the blocks differ
    assert text.endswith(f"Page {page + 1} of 3")


def test_twelve_picks_split_five_four_three_read_twelve_on_each_page():
    snap = snapshot(daily=12)
    counts = []
    for page in range(3):
        text, _ = pagination.render_page(snap, "daily", page, 4, LAGOS, now=NOW)
        counts.append((text.count("💰"), "12 picks" in text))
    assert counts == [(4, True), (4, True), (4, True)]
    sizes = [pagination.render_page(snap, "daily", pg, 5, LAGOS, now=NOW)[0].count("💰") for pg in range(3)]
    assert sizes == [5, 5, 2]


def test_neither_card_mentions_the_others_figures():
    snap = snapshot()
    top, _ = pagination.render_page(snap, "top", 0, 5, LAGOS, now=NOW)
    daily, _ = pagination.render_page(snap, "daily", 0, 5, LAGOS, now=NOW)
    assert "14.55" not in top and "2.00" not in daily and "· 14 picks" not in top and "· 2 picks" not in daily
    assert "Chelsea" not in daily and "Home 21" not in top


@pytest.mark.parametrize("reader,label", [("Africa/Lagos", "WAT"), ("Europe/London", "BST"), ("America/New_York", "EDT")])
def test_the_header_names_the_readers_zone_never_a_hardcoded_wat(reader, label):
    text, _ = pagination.render_page(snapshot(), "top", 0, 5, "Africa/Lagos", user_tz=reader, now=NOW)
    assert f"🕘 times in {label}" in text
    assert ("WAT" in text) == (label == "WAT")
    assert re.findall(r"🕘 (\d+:\d\d [AP]M)", text) and "WAT" not in re.sub(r"times in \w+", "", text)


def test_the_channel_post_uses_the_configured_zone():
    from predbot.bot.format import picks_message
    from tests.test_picks_format import event, sc
    card = picks_message([sc(event(1), "Home Win", 0.70, 1.60)], [], datetime(2026, 9, 16, 9, tzinfo=timezone.utc),
                         "America/New_York", None)
    assert "🕘 times in EDT" in card and "WAT" not in card


def test_zone_label_uses_the_abbreviation_in_force_on_the_cards_day_and_falls_back_to_the_iana_name(monkeypatch):
    assert zone_label("Europe/London", "2030-01-15") == "GMT" and zone_label("Europe/London", "2030-07-15") == "BST"
    from predbot.bot import format as fmt
    monkeypatch.setattr(fmt, "to_local", lambda dt, tz: SimpleNamespace(tzname=lambda: "+01"))
    assert zone_label("Africa/Lagos", "2030-06-01") == "Africa/Lagos"                 # numeric name: use the IANA one


def test_a_match_under_way_appends_a_red_circle_after_its_time():
    started = broadcast.picks_text(snapshot(), LAGOS, now=datetime(2030, 6, 1, 17, 0, tzinfo=timezone.utc))
    assert "🕘 5:00 PM 🔴" in started and started.count("🔴") >= 1
    assert "Started" not in started
    early = broadcast.picks_text(snapshot(), LAGOS, now=NOW)
    assert "🔴" not in early


# ------------------------------------------------------------------------------------------------ the vocabulary
ALLOWED = set("🔥📋🎟🎯💰📊🕘🔴✅❌➖🏆📈🗓🏅💹⚽🏀🏈⚾🎾🥊") | {"️", "↩", "◀", "▶", "↓", "•", "·", "—", "–"}


def emoji_of(text):
    return {c for c in text if ord(c) > 0x2100 or c in "•·—–"} - {c for c in text if c in "•·—–"}


def test_cards_use_only_the_allowed_emoji():
    snap = snapshot()
    texts = [pagination.render_page(snap, v, p, 5, LAGOS, now=NOW)[0] for v in ("top", "daily") for p in (0, 1)]
    texts.append(broadcast.picks_text(snap, LAGOS, now=NOW))
    texts += [pagination.render_page(DailySnapshot(date=DAY), "top", 0, 5, LAGOS)[0]]
    for text in texts:
        assert emoji_of(text) <= ALLOWED, emoji_of(text) - ALLOWED


def test_help_and_toasts_use_only_the_allowed_emoji():
    from predbot.bot.callbacks import _new_toast, commands_text
    from predbot.bot.commands import CommandLoop
    from predbot.config import Config
    from tests.test_subscribers_broadcast import FakeTG
    loop = CommandLoop(FakeTG(), SimpleNamespace(subscriber_timezone=lambda c: ""), Config(timezone=LAGOS), {})
    help_text = loop.handle("/help", "1", "1")
    assert emoji_of(help_text + commands_text()) <= ALLOWED
    service = SimpleNamespace(get_daily_snapshot=snapshot, snapshot_for=lambda d: snapshot())
    for view in ("top", "daily", "stats", "yesterday"):
        assert emoji_of(_new_toast(view, 0, False, service, None, 5)) <= ALLOWED
    assert emoji_of(_new_toast("daily", 1, True, service, DAY, 5)) <= ALLOWED


# ------------------------------------------------------------------------------------------------ delivery never cuts a block
def _long_picks(n, tier="daily"):
    label = "A Very Long Outcome Label That Goes On " * 3
    league = "Some Confederation • Some Extremely Long Competition Name"
    return [rec(i, tier, 12 + i % 10, league=league, home="Wolverhampton Wanderers Football Club Reserves " + str(i),
                away="Brighton & Hove Albion <Women> " + str(i), label=label + str(i)) for i in range(n)]


def test_a_page_of_the_longest_labels_stays_well_under_the_telegram_limit():
    snap = DailySnapshot(date=DAY, daily_picks=_long_picks(pagination.PAGE_SIZE_DEFAULT))
    text, _ = pagination.render_page(snap, "daily", 0, pagination.PAGE_SIZE_DEFAULT, LAGOS, now=NOW)
    assert len(text) < 4096 - 300
    delivered = broadcast.picks_text(snap, LAGOS, now=NOW)
    assert len(delivered) < 4096


def test_split_cuts_only_between_pick_blocks_and_keeps_the_header_with_its_first_block():
    snap = DailySnapshot(date=DAY, top_picks=_long_picks(9, "top"), daily_picks=_long_picks(20))
    text = broadcast.picks_text(snap, LAGOS, now=NOW)
    assert len(text) > MAX_LEN
    parts = _split(text)
    assert len(parts) >= 2 and all(len(p) <= MAX_LEN for p in parts)
    assert "\n\n".join(parts) == text                                     # nothing lost, nothing added
    blocks = [b for b in text.split("\n\n") if b.startswith("⚽")]
    for part in parts:
        for block in part.split("\n\n"):
            if block.startswith("⚽"):
                assert block in blocks and block.count("\n") == 2          # every block whole: three lines
        assert not re.match(r"^(🔥 TOP PICKS|📋 DAILY PICKS) ·[^\n]*\n[^\n]*$", part.split("\n\n")[-1])   # no stranded header


def test_split_never_lands_inside_an_html_tag_or_entity():
    long_line = ("word " * 400 + "<b>bold text</b> &amp; more " + "x" * 50) * 3
    for piece in _split(long_line, 500):
        assert len(piece) <= 500
        assert piece.count("<") == piece.count(">") and not re.search(r"&[a-z]*$", piece)


def test_telegram_send_uses_the_block_aware_split(monkeypatch):
    bodies = []

    class Session:
        def post(self, url, json=None, timeout=None):
            bodies.append(json["text"])
            return SimpleNamespace(status_code=200, json=lambda: {"result": {"message_id": len(bodies)}}, text="")

    tg = Telegram("token", "1")
    tg.session = Session()
    snap = DailySnapshot(date=DAY, daily_picks=_long_picks(24))
    text = broadcast.picks_messages(snap, LAGOS, now=NOW)[1][1]                # the Daily message
    assert tg.send(text, chat_id="9")
    assert len(bodies) >= 2 and all(len(b) <= MAX_LEN for b in bodies)
    assert all(b.startswith("⚽") or b.startswith("📋") for b in bodies)      # every message starts at a block boundary
