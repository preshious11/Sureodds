"""Integration: persisted selection -> publication -> live tracking -> match-start event -> settlement ->
match-end event -> results recap, with All-only predictions in the data and proven excluded at every stage.

Generation and Top/Daily *selection* have their own suites (thresholds and engine are deliberately untouched here);
this test starts from the persisted outcome of selection, which is what every later stage reads."""
from datetime import timedelta
from unittest.mock import Mock

from predbot.bot.format import compact_recap_message
from predbot.config import Config
from predbot.jobs import live_results, settle, stats
from predbot.providers.base import MatchResult
from predbot.providers.chain import ResultsResolver
from tests.test_live_results import DAY, NOW, Provider, Telegram, pick


def _setup(db):
    forbidden = Mock()
    for method in ("begin_run", "fetch_result", "find_result", "fetch_events"):
        getattr(forbidden, method).side_effect = AssertionError("paid/prediction call during live polling")
    provider = Provider()
    resolver = ResultsResolver({"espn": provider, "oddsapi": forbidden, "sportsgameodds": forbidden})
    return provider, resolver, Config(live_updates_enabled=True, live_poll_minutes=5), Telegram()


def _slate(db):
    """Persist a day: Top-only, Daily-only, Top+Daily (two selections), All-only fixture, All-only sibling."""
    ids = {}
    ids["top_only"] = pick(db, home="Top FC", away="TopAway", provider_event_id="e1", label="Top FC to Win")
    ids["daily_only"] = pick(db, tier="daily", home="Daily FC", away="DailyAway", provider_event_id="e2",
                             selection="away", label="DailyAway to Win")           # will lose (home wins 2-1)
    ids["both_top"] = pick(db, home="Both FC", away="BothAway", provider_event_id="e3", label="Both FC to Win")
    ids["both_daily"] = pick(db, tier="daily", home="Both FC", away="BothAway", provider_event_id="e3",
                             market="total", selection="over", line=1.5, label="Over 1.5 Total Goals")
    ids["all_only_fixture"] = pick(db, tier="all", home="Hidden FC", away="HiddenAway", provider_event_id="e4",
                                   label="Hidden FC to Win")
    ids["all_only_sibling"] = pick(db, tier="all", home="Top FC", away="TopAway", provider_event_id="e1",
                                   market="total", selection="over", line=1.5, label="Over 1.5 Total Goals")
    return ids


def test_published_only_lifecycle(db):
    provider, resolver, cfg, tg = _setup(db)
    ids = _slate(db)
    db.mark_post("top", DAY, "m1")      # publication: the cards were posted
    db.mark_post("daily", DAY, "m2")

    events = []
    sink = lambda *args: events.append(args)  # noqa: E731

    # --- kickoff: tracked and announced for published fixtures only --------------------------------------------
    provider.result = MatchResult("in_progress", source="espn")
    live_results.run(resolver, db, cfg, tg, now=NOW, sink=sink)
    starts = [e for e in events if e[2] == "start"]
    assert len(starts) == 3, "Top-only, Daily-only and the Top+Daily fixture - once each"
    text = "\n".join(e[3] for e in starts)
    assert "Hidden" not in text and "HiddenAway" not in text
    assert text.count("Over 1.5 Total Goals") == 1, "only the published Over 1.5 (Both FC); the All-only sibling is hidden"
    assert not tg.messages, "GitHub Actions never sends Telegram; the Worker owns delivery"

    # --- full time: settlement + end events for published fixtures only ---------------------------------------
    provider.result = MatchResult("final", {"full": (2., 1.)}, source="espn")
    live_results.run(resolver, db, cfg, tg, now=NOW + timedelta(minutes=125), sink=sink)
    ends = [e for e in events if e[2] == "end"]
    assert len(ends) == 3
    assert all("Hidden" not in e[3] for e in ends)
    status = {r["id"]: r["status"] for r in db.picks_for_date(DAY)}
    assert status[ids["top_only"]] == "won" and status[ids["daily_only"]] == "lost"
    assert status[ids["both_top"]] == "won" and status[ids["both_daily"]] == "won"
    assert status[ids["all_only_fixture"]] == "pending", "an All-only prediction is never settled"
    assert status[ids["all_only_sibling"]] == "pending", "nor is an All-only sibling of a published fixture"

    # --- the ordinary settlement job also leaves All-only alone ------------------------------------------------
    assert ids["all_only_fixture"] not in {r["id"] for r in db.pending_picks(published_only=True)}
    assert ids["all_only_fixture"] in {r["id"] for r in db.pending_picks()}, "still generated and visible"

    # --- statistics and the recap exclude All-only ------------------------------------------------------------
    from predbot.util.timeutil import parse_iso
    day_after = parse_iso(f"{DAY}T10:00:00Z") + timedelta(days=1)
    s = stats.compute(db, day_after)
    assert s["settled_ever"] == 4, "four published settled predictions; the two All-only ones do not count"
    top = [r["status"] for r in sorted(db.collection_predictions("top", DAY), key=lambda r: r["collection_rank"])]
    daily = [r["status"] for r in sorted(db.collection_predictions("daily", DAY), key=lambda r: r["collection_rank"])]
    recap = compact_recap_message(top, daily)
    lines = recap.split(chr(10))
    assert lines[:8] == ["📊 SUREODDS RESULTS", "", "🏆 BET OF THE DAY", "", "✅ ✅", "", "2 Wins | 0 Losses", ""]
    assert lines[8] == "⚽ DAILY PICKS" and sorted(lines[10].split()) == ["✅", "❌"] and lines[12] == "1 Win | 1 Loss"
    assert "FC" not in recap and "@" not in recap


def test_recap_marks_void_push_and_half_results_without_counting_them_as_wins_or_losses():
    text = compact_recap_message(["won", "push", "void"], ["half_won", "half_lost", "lost", "pending"])
    assert "✅ ↩️ ➖" in text and "✅½ ❌½ ❌ ⏳" in text
    assert "1 Win | 0 Losses | 1 Push | 1 Void" in text
    assert "0 Wins | 1 Loss | 1 Half Win | 1 Half Loss | 1 Pending" in text
    assert compact_recap_message([], []) is None


def test_expired_published_selection_is_settled_but_all_only_is_not(db):
    pub = pick(db)
    hidden = pick(db, tier="all", home="X", away="Y", provider_event_id="e9")
    counts = settle.expire_pending(db, NOW + timedelta(days=5))
    status = {r["id"]: r["status"] for r in db.picks_for_date(DAY)}
    assert status[pub] == "void" and status[hidden] == "pending"
    assert counts["void"] == 1
