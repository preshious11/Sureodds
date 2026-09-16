"""The daily predictions card: format.picks_message / broadcast.picks_text."""
from __future__ import annotations

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

    assert picks_message([villa], [overs], DAY, WAT, None) == (
        "<b>Today's</b> predictions—— <b>2026-09-16</b>\n"
        "\n"
        "⚽ Football\n"
        "Home1 vs Away1\n"
        "Aston Villa to win\n"
        "Confidence: <b>72%</b> | <b>07:45 PM WAT</b>\n"
        "2.05\n"
        "\n"
        "⚽ Football\n"
        "Home2 vs Away2\n"
        "Over 1.5 Goals\n"
        "Confidence: <b>84%</b> | <b>03:00 PM WAT</b>\n"
        "1.74\n"
        "\n"
        "Combined odds: <b>3.57</b>"
    )


def test_top_picks_are_listed_first_then_the_daily_ones():
    top = [sc(event(1), "Top One", 0.70, 1.80)]
    daily = [sc(event(2), "Daily One", 0.60, 1.90, market="total", selection="over", line=2.5),
             sc(event(3), "Daily Two", 0.55, 2.00, selection="away")]

    card = picks_message(top, daily, DAY, WAT, None)

    assert card.index("Home1 vs Away1") < card.index("Home2 vs Away2") < card.index("Home3 vs Away3")
    assert card.count("Top One") == 1 and card.count("Daily One") == 1 and card.count("Daily Two") == 1


def test_every_pick_gets_the_five_line_block():
    card = picks_message([sc(event(1, "Serie A"), "Inter to win", 0.66, 1.95)], [], DAY, WAT, None)

    assert card.splitlines() == [
        "<b>Today's</b> predictions—— <b>2026-09-16</b>",
        "",
        "⚽ Football",
        "Home1 vs Away1",
        "Inter to win",
        "Confidence: <b>66%</b> | <b>07:45 PM WAT</b>",
        "1.95",
        "",
        "Combined odds: <b>1.95</b>",
    ]


def test_empty_day_has_a_placeholder_and_no_combined_odds():
    card = picks_message([], [], DAY, WAT, None)

    assert "No selection cleared the quality bar today." in card
    assert "Combined odds" not in card


def test_league_falls_back_to_the_sport_name_and_unknown_kickoff_is_dashed():
    card = picks_message([sc(event(1, league=""), "Home Win", 0.60, 1.50)], [], DAY, WAT, None)

    assert "⚽ Football\n" in card
    lines = card.splitlines()
    assert lines[2] == "⚽ Football"
    assert lines[3] == "Home1 vs Away1"  # league omitted
    assert kickoff_label(None, WAT) == "--:--"


def test_match_and_outcome_are_html_escaped():
    ev = event(1, league="Copa & Liga", home="Benfica <B>", away="Away & Co")
    card = picks_message([sc(ev, "Home <win> & draw", 0.60, 1.50)], [], DAY, WAT, None)

    lines = card.splitlines()
    assert "Benfica &lt;B&gt; vs Away &amp; Co" in lines  # match line escaped
    assert "Home &lt;win&gt; &amp; draw" in lines
    assert "Copa" not in card


def test_combined_odds_is_the_product_of_every_listed_pick():
    assert combined_odds([1.5, 2.0]) == 3.0
    assert combined_odds([]) == 1.0


def test_record_footer_stays_last_when_supplied():
    card = picks_message([sc(event(1), "Home Win", 0.60, 1.50)], [], DAY, WAT, "\U0001F4CA 7d: 1W-0L")

    assert card.endswith("\U0001F4CA 7d: 1W-0L")


# ------------------------------------------------------- subscriber DM parity
def test_subscriber_card_matches_the_channel_card():
    top = [sc(event(1, "Premier League"), "Aston Villa to win", 0.72, 2.05)]
    daily = [sc(event(2, "La Liga", kickoff="2026-09-16T14:00:00Z"), "Over 1.5 Goals", 0.84, 1.74,
                market="total", selection="over", line=1.5)]
    snapshot = DailySnapshot(
        date="2026-09-16",
        top_picks=[record(1, "top", "Premier League", "Aston Villa to win", 0.72, 2.05)],
        daily_picks=[record(2, "daily", "La Liga", "Over 1.5 Goals", 0.84, 1.74,
                            kickoff="2026-09-16T14:00:00Z",
                            market="total", selection="over", line=1.5)],
    )

    assert picks_text(snapshot, WAT) == picks_message(top, daily, DAY, WAT, None)


def test_subscriber_card_appends_the_7_day_record_when_history_exists():
    snapshot = DailySnapshot(
        date="2026-09-16",
        top_picks=[record(1, "top", "Premier League", "Aston Villa to win", 0.72, 2.05)],
        stats=StatsSummary.from_raw({"last7": {"won": 15, "lost": 12, "roi": 8.0, "n": 27},
                                     "all": {"won": 15, "lost": 12, "roi": 8.0, "n": 27}}),
    )

    last = picks_text(snapshot, WAT).splitlines()[-1]

    assert last.startswith("\U0001F4CA 7d: 15W-12L")
    assert last.endswith("ROI +8.0%")