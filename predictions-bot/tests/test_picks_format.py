"""The daily predictions card: format.picks_message / broadcast.picks_text."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from predbot.bot.broadcast import picks_text
from predbot.bot.format import combined_odds, kickoff_label, picks_message
from predbot.cache.models import DailySnapshot, PickRecord, StatsSummary
from predbot.model.ensemble import Scored
from predbot.providers.base import Event, MarketLine

WAT = "Africa/Lagos"
DAY = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


def event(i, league="Premier League", sport="football", kickoff="2026-09-16T18:45:00Z",
          home=None, away=None):
    return Event(provider="test", provider_event_id=str(i), sport=sport, league=league,
                 home=home or f"Home{i}", away=away or f"Away{i}",
                 start=datetime.strptime(kickoff, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
                 sources=["a", "b"])


def sc(event_, label, p_model, price, market="result3", selection="home", line=None):
    ml = MarketLine(market, selection, price, n_books=2, line=line)
    return Scored(event=event_, line=ml, p_market=p_model - 0.02, p_model=p_model,
                  sources={"a": p_model, "b": p_model}, agreement=1.0, ev=0.05,
                  edge=0.03, label=label, liquidity=1.0)


def record(pick_id, tier, league, label, prob, price, kickoff="2026-09-16T18:45:00Z",
           market="result3", selection="home", line=None):
    return PickRecord(pick_id=pick_id, tier=tier, rank=pick_id, sport="football", league=league,
                      home=f"Home{pick_id}", away=f"Away{pick_id}", kickoff_utc=kickoff,
                      label=label, price=price, prob=prob, pick_date="2026-09-16",
                      market=market, selection=selection, line=line)


# --------------------------------------------------------------------- the card
def test_card_matches_the_requested_layout():
    villa = sc(event(1, "Premier League", kickoff="2026-09-16T18:45:00Z"),
               "Aston Villa to win", 0.72, 2.05)
    overs = sc(event(2, "La Liga", kickoff="2026-09-16T14:00:00Z"),
               "Over 1.5 Goals", 0.84, 1.74, market="total", selection="over", line=1.5)

    assert picks_message([villa], [overs], DAY, WAT) == (
        "🔥 BET OF THE DAY · Wed 16 Sep\n"
        "🎟 Combined odds 2.05 · 1 pick · 🕘 times in WAT\n"
        "\n"
        "⚽ Premier League · Home1 vs Away1\n"
        "🎯 Aston Villa to win\n"
        "💰 2.05  ·  📊 72%  ·  🕘 7:45 PM\n\nBet only what you can afford to lose.\n"
        "\n"
        "📋 DAILY PICKS · Wed 16 Sep\n"
        "🎟 Combined odds 1.74 · 1 pick · 🕘 times in WAT\n"
        "\n"
        "⚽ La Liga · Home2 vs Away2\n"
        "🎯 Over 1.5 Goals\n"
        "💰 1.74  ·  📊 84%  ·  🕘 3:00 PM\n\nBet only what you can afford to lose."
    )


def test_top_picks_are_listed_first_then_the_daily_ones():
    top = [sc(event(1), "Top One", 0.70, 1.80)]
    daily = [sc(event(2), "Daily One", 0.60, 1.90, market="total", selection="over", line=2.5),
             sc(event(3), "Daily Two", 0.55, 2.00, selection="away")]

    card = picks_message(top, daily, DAY, WAT)

    assert card.index("Home1 vs Away1") < card.index("Home2 vs Away2") < card.index("Home3 vs Away3")
    assert card.count("Top One") == 1 and card.count("Daily One") == 1 and card.count("Daily Two") == 1


def test_every_pick_gets_the_three_line_block():
    card = picks_message([], [sc(event(1, "Serie A"), "Inter to win", 0.66, 1.95)], DAY, WAT)

    assert card.splitlines() == [
        "📋 DAILY PICKS · Wed 16 Sep",
        "🎟 Combined odds 1.95 · 1 pick · 🕘 times in WAT",
        "",
        "⚽ Serie A · Home1 vs Away1",
        "🎯 Inter to win",
        "💰 1.95  ·  📊 66%  ·  🕘 7:45 PM", "", "Bet only what you can afford to lose.",
    ]


def test_picks_are_listed_earliest_kickoff_first():
    late = sc(event(1, kickoff="2026-09-16T20:00:00Z"), "Late", 0.60, 1.50)
    early = sc(event(2, kickoff="2026-09-16T12:00:00Z"), "Early", 0.60, 1.60, selection="away")
    mid = sc(event(3, kickoff="2026-09-16T16:00:00Z"), "Mid", 0.60, 1.70, market="total", selection="over", line=2.5)

    card = picks_message([], [late, early, mid], DAY, WAT)

    assert card.index("Early") < card.index("Mid") < card.index("Late")


def test_evaluated_empty_day_has_honest_copy_and_no_combined_odds():
    card = picks_message([], [], DAY, WAT)

    assert "No selections qualified today." in card
    assert "Combined odds" not in card


def test_league_falls_back_to_the_sport_name_and_unknown_kickoff_is_dashed():
    card = picks_message([sc(event(1, league=""), "Home Win", 0.60, 1.50)], [], DAY, WAT)

    lines = card.splitlines()
    assert lines[3] == "⚽ Football · Home1 vs Away1"       # league falls back to the sport name
    assert kickoff_label(None, WAT) == "--:--"


def test_match_league_and_outcome_are_html_escaped():
    ev = event(1, league="Copa & Liga", home="Benfica <B>", away="Away & Co")
    card = picks_message([sc(ev, "Home <win> & draw", 0.60, 1.50)], [], DAY, WAT)

    lines = card.splitlines()
    assert "⚽ Copa &amp; Liga · Benfica &lt;B&gt; vs Away &amp; Co" in lines  # league + match line escaped
    # Outcome is now in prediction line
    assert any("Home &lt;win&gt; &amp; draw" in line for line in lines)  # outcome line escaped
    assert "<win>" not in card                     # never raw, it would break parse_mode


def test_combined_odds_is_the_product_of_every_listed_pick():
    assert combined_odds([1.5, 2.0]) == 3.0
    assert combined_odds([]) == 1.0


# ------------------------------------------------------- subscriber DM parity
def test_subscriber_summary_is_the_canonical_card_and_matches_the_views():
    top = [record(1, "top", "Premier League", "Aston Villa to win", 0.72, 2.05)]
    daily = [record(2, "daily", "La Liga", "Over 1.5 Goals", 0.84, 1.74,
                    kickoff="2026-09-16T14:00:00Z",
                    market="total", selection="over", line=1.5)]
    snapshot = DailySnapshot(
        date="2026-09-16",
        top_picks=top,
        daily_picks=daily,
    )

    card = picks_text(snapshot, WAT)

    # the exact canonical summary structure, one section per ticket, each with its own odds and count
    assert card.startswith("🔥 BET OF THE DAY · Wed 16 Sep\n🎟 Combined odds 2.05 · 1 pick · 🕘 times in WAT")
    assert "📋 DAILY PICKS · Wed 16 Sep\n🎟 Combined odds 1.74 · 1 pick · 🕘 times in WAT" in card
    # every pick block is byte-identical to the paginated view's card
    from predbot.bot.pagination import render_pick

    for p in snapshot.top_picks + snapshot.daily_picks:
        assert render_pick(p, WAT) in card


def test_no_picks_card_carries_a_results_line_even_when_history_exists():
    """The 7-day record used to close the delivered card; results now live only in /stats, /yesterday and the recap."""
    snapshot = DailySnapshot(
        date="2026-09-16",
        top_picks=[record(1, "top", "Premier League", "Aston Villa to win", 0.72, 2.05)],
        daily_picks=[record(2, "daily", "La Liga", "Over 1.5 Goals", 0.84, 1.74, market="total", selection="over", line=1.5)],
        stats=StatsSummary.from_raw({"last7": {"won": 15, "lost": 12, "roi": 8.0, "n": 27},
                                     "all": {"won": 15, "lost": 12, "roi": 8.0, "n": 27}, "streak": "W3"}),
    )
    from predbot.bot.pagination import render_page
    texts = [text for _, text, _ in broadcast_messages(snapshot)] + [picks_text(snapshot, WAT)]
    texts += [render_page(snapshot, v, 0, 5, WAT)[0] for v in ("top", "daily")]
    for text in texts:
        assert not re.search(r"7d|ROI|\d+W-\d+L|\bW-|Yesterday|streak", text), text
        assert "✅" not in text and "❌" not in text and "➖" not in text
    assert picks_message([sc(event(1), "Home Win", 0.60, 1.50)], [], DAY, WAT).count("📊") == 1     # only the confidence icon


def broadcast_messages(snapshot):
    from predbot.bot.broadcast import picks_messages
    return picks_messages(snapshot, WAT)
