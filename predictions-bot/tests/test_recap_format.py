"""The recap card (bot.format.recap_message over jobs.stats.compute); the numbers themselves are tested in test_stats_card."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from predbot.bot.format import recap_message
from predbot.config import Config
from predbot.jobs import recap, stats as stats_mod

TODAY = datetime(2030, 6, 2, 9, 30, tzinfo=timezone.utc)      # local "today"
YESTERDAY = "2030-06-01"


def _pick(db, *, date=YESTERDAY, status="won", sport="football", label="Home Win",
          price=2.00, rank=1, tier="daily") -> int:
    pid = db.insert_pick({
        "pick_date": date, "tier": tier, "rank": rank, "provider": "fake",
        "provider_event_id": f"{date}-{rank}-{label}", "sport": sport, "league": "EPL",
        "home": f"Geylang International FC {rank} {label}", "away": "Away FC",     # one match per pick (unique per date+fixture+selection)
        "start_utc": f"{date}T12:00:00Z", "market": "result3", "period": "full",
        "stat": "points", "entity": "home", "line": None, "selection": "home",
        "label": label, "price": price, "prob": 0.70, "ev": 0.05, "p_market": 0.60,
        "sources": {"market": 0.60},
    })
    if status != "pending":
        db.settle_pick(pid, status)
    return pid


def _card(db, today=TODAY) -> str:
    return recap_message(stats_mod.compute(db, today))


# ------------------------------------------------------------------- the layout
def test_card_matches_the_requested_layout(db):
    # yesterday: four football winners; the 1.44 one is the day's best pick
    for i, (price, label) in enumerate([(1.30, "Home Win"),
                                        (1.35, "Over 2.5 goals"),
                                        (1.40, "Both Teams To Score - Yes"),
                                        (1.44, "Geylang International FC Win")], start=1):
        _pick(db, rank=i, price=price, label=label)
    # earlier in the 7-day window: 11 more wins + 12 losses
    for i in range(11):
        _pick(db, date="2030-05-28", rank=100 + i, price=2.00)
    for i in range(12):
        _pick(db, date="2030-05-28", rank=200 + i, status="lost", price=2.00)

    text = _card(db)
    assert text.startswith("📊 RESULTS · Sat 1 Jun\n\n🗓 Yesterday · Sat 1 Jun · 4 picks\n✅ 4  ❌ 0  ➖ 0\n")
    assert "🎯 100%  ·  💹 ROI +" in text
    assert "🏆 Best: Geylang International FC Win · Geylang International FC 4 Geylang International FC Win vs Away FC · @1.44" in text
    assert "🏅 By sport\n⚽ Football 4/4" in text
    assert "📈 Last 5 days\n✅ 15  ❌ 12  ➖ 0  ·  🎯 56%" in text                # 05-28 .. 06-01, first pick on 05-28


def test_void_and_push_are_listed_but_not_counted_as_decided(db):
    _pick(db, rank=1, price=1.50)                       # won
    _pick(db, rank=2, price=1.90, status="void")        # ➖
    _pick(db, rank=3, price=1.95, status="push")        # ↩️

    text = _card(db)

    assert "✅ 1  ❌ 0  ➖ 1  ↩️ 1" in text
    assert "🎯 100%" in text                             # void and push are not decided
    assert "⚽ Football 1/3" in text                     # settled = 3 of 3 issued
    assert "🏆 Best:" in text


def test_pending_only_day_says_so_and_shows_no_percentages_or_best_pick(db):
    _pick(db, status="pending")

    text = _card(db)

    assert "🗓 Yesterday · Sat 1 Jun · 1 pick — results come in after the last match ends." in text
    assert "🎯" not in text and "🏆" not in text and "⏳" not in text
    assert "⚽" not in text                              # no per-sport line until something settles


# ------------------------------------------------------------ sport breakdown
def test_sport_breakdown_lists_every_sport_with_won_over_settled(db):
    _pick(db, rank=1, sport="football")
    _pick(db, rank=2, sport="football", status="lost")
    _pick(db, rank=3, sport="football", status="half_won")     # halves count as wins
    _pick(db, rank=4, sport="basketball")
    _pick(db, rank=5, sport="baseball", status="pending")
    _pick(db, rank=6, sport="american_football", status="void")

    lines = [ln for ln in _card(db).splitlines() if ln.startswith("⚽")]

    assert lines == ["⚽ Football 2/3  ·  🏈 American Football 0/1  ·  🏀 Basketball 1/1  ·  ⚾ Baseball —"]


def test_by_sport_ties_sort_alphabetically():
    rows = [{"status": "won", "sport": s} for s in ("tennis", "mma", "baseball")]
    assert [b["sport"] for b in stats_mod.by_sport(rows)] == ["baseball", "mma", "tennis"]


def test_by_sport_ignores_unknown_statuses():
    rows = [{"status": "won", "sport": "football"}, {"status": "weird", "sport": "football"}]
    assert stats_mod.by_sport(rows) == [{"sport": "football", "won": 1, "lost": 0, "n": 1}]


# --------------------------------------------------------------------- escaping
def test_best_pick_label_is_html_escaped(db):
    _pick(db, rank=1, label="Brighton & Hove <Win>", price=1.60)
    assert "Brighton &amp; Hove &lt;Win&gt;" in _card(db)


# ------------------------------------------------------------------ job wiring
def test_recap_card_serves_the_summary_card(db):
    from predbot.bot.commands import recap_card

    now_utc = datetime.now(timezone.utc)
    y = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    _pick(db, date=y, label="Geylang International FC Win", price=1.44)

    text = recap_card(db, now_utc)

    assert text.startswith("📊 RESULTS · ")
    assert "✅ 1  ❌ 0  ➖ 0" in text and "🎯 100%" in text
    assert "Geylang International FC Win" in text and "@1.44" in text


def test_recap_job_renders_the_new_card(db, capsys):
    today_utc = datetime.now(timezone.utc)
    y = (today_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    _pick(db, date=y, label="Geylang International FC Win", price=1.44)

    text = recap.run(db, Config(timezone="UTC"), None, dry_run=True)

    assert text.startswith("📊 RESULTS · ")
    assert "⚽ Football 1/1" in text
    assert "🏆 Best:" in text
    assert text in capsys.readouterr().out
