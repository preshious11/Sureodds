"""Internal prediction analytics must never reach a subscriber.

The evidence / sharp-reference / accuracy values (fair probability, edge, rank score, book and provider counts, market
quality, uncertainty, Brier, calibration, CLV, why-a-pick-was-chosen) are for the engine, the database, logs, the CLI and
the admin ``/lastrun``. These tests seed DISTINCTIVE canary values into every one of those fields, drive every real
subscriber path (/start, /top, /daily, /today, /stats, /yesterday, every pagination callback, the scheduled/broadcast
cards, the recap), and fail if a canary - or diagnostic wording - shows up in any message text or button."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from predbot.bot import broadcast, callbacks, format as fmt, pagination
from predbot.bot.callbacks import handle_callback, handle_command, handle_start
from predbot.cache.fetcher import UnifiedFetcher
from predbot.cache.models import DailySnapshot, PickRecord, VIEW_DAILY, VIEW_STATS, VIEW_TOP, VIEW_YESTERDAY
from predbot.config import Config
from predbot.jobs import daily_picks
from predbot.jobs import stats as stats_mod
from predbot.selection.picker import select
from tests.conftest import dt
from tests.test_picker import _fx
from tests.test_prediction_quality import cand
from tests.test_subscribers_broadcast import FakeTG

NOW = dt(hh=18)
TZ = "Africa/Lagos"

#: values that exist ONLY in internal fields; none can occur in a legitimate card or stats message
CANARY_FAIR, CANARY_EDGE = 0.73913, 0.06137
CANARY_STRINGS = ["73913", "0.7391", "73.9", "06137", "0.0613", "6.1%", "canarybook", "canary_signal", "pinnacle",
                  "1717", "0.31337", "31337"]

#: words an internal diagnostic would use. ("quality bar" is the existing empty-card copy, removed before matching.)
FORBIDDEN = re.compile(
    r"p_fair|edge_ev|rank_score|rank score|fair prob|fair value|\bedge\b|\bEV\b|expected value|\bbooks?\b|bookmaker|"
    r"\bproviders?\b|\bsharp\b|pinnacle|betfair|brier|calibrat|\bclv\b|closing line|closing price|uncertaint|"
    r"evidence|market quality|\brestricted\b|\bsecondary\b|p_market|de-?vig|lower bound|confidence bound|"
    r"why (it|this|the pick)|passed|rejected|\bdispersion\b|\bse\b", re.IGNORECASE)


def _scrub(text: str) -> str:
    return text.replace("No selection cleared the quality bar today.", "")


def _seen(tg: FakeTG) -> str:
    """Everything a subscriber's chat received: message text AND every button label / callback payload."""
    parts = []
    for m in tg.sends + tg.edits:
        parts.append(str(m.get("text", "")))
        parts.append(json.dumps(m.get("reply_markup") or {}, ensure_ascii=False))
    parts += [str(a.get("text") or "") for a in tg.answers]
    return "\n".join(parts)


def assert_clean(text: str, where: str) -> None:
    body = _scrub(text)
    for canary in CANARY_STRINGS:
        assert canary not in body, f"internal value {canary!r} leaked into {where}"
    hit = FORBIDDEN.search(body)
    assert hit is None, f"diagnostic wording {hit.group(0)!r} leaked into {where}: ...{body[max(0, hit.start() - 40):hit.end() + 40]!r}"


# --------------------------------------------------------------------------------------- a real generated day
def _canary_pool():
    """Well-covered main-market candidates carrying sharp-reference data, so every internal field is populated."""
    pool = []
    for i in range(14):
        quotes = {f"canarybook{k}": round(1.45 + 0.003 * k, 4) for k in range(5)}
        quotes["pinnacle"] = 1.44
        s = cand(_fx(i), selection="home" if i % 2 == 0 else "away", p=0.70, price=1.45, quotes=quotes,
                 p_fair=CANARY_FAIR, edge_ev=CANARY_EDGE, sources=("market", "score_model", "sharp", "canary_signal"),
                 independent=("sharp",))
        s.p_market = 0.31337
        s.notes["canary"] = "canary_signal"
        pool.append(s)
    return pool


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr("predbot.cache.fetcher.utcnow", lambda: NOW)
    monkeypatch.setattr(daily_picks, "utcnow", lambda: NOW)
    ps = select(_canary_pool(), Config(), NOW)
    assert ps.top and ps.daily, "the canary slate must exercise both tiers"
    monkeypatch.setattr(daily_picks, "build_picks", Mock(side_effect=lambda *a, **k: ps))
    cfg = Config(timezone=TZ, telegram_admin_ids=["999"])
    fetcher = UnifiedFetcher(Mock(), db, cfg)
    snapshot = fetcher.get_daily_snapshot(scheduled=True)
    # settled history (with internal analytics stored on every row) for /stats, /yesterday and the recap
    for n, status in enumerate(("won", "won", "lost", "won")):
        db.insert_pick({"pick_date": "2030-05-31", "tier": "daily" if n else "top", "rank": n + 1, "provider": "t",
                        "provider_event_id": f"old{n}", "sport": "football", "league": "EPL", "home": f"OldH{n}",
                        "away": f"OldA{n}", "start_utc": "2030-05-31T17:00:00Z", "market": "result3", "period": "full",
                        "stat": "points", "entity": "all", "line": None, "selection": "home", "label": "x",
                        "price": 1.5, "prob": 0.62, "ev": 0.4242, "p_market": 0.31337, "prob_raw": 0.6,
                        "p_fair": CANARY_FAIR, "edge_ev": CANARY_EDGE, "rank_score": 0.6613, "books": 1717,
                        "quality": "main", "closing_price": 1.4, "clv": 0.0714, "status": status,
                        "settled_at": "2030-05-31T21:00:00Z"})
    return fetcher, db, cfg, snapshot


# --------------------------------------------------------------------------------------- the paths
def test_the_canary_values_really_are_stored_internally(world):
    """The leak tests below mean something only if the internal data exists - in the DB and in the engine."""
    _, db, _, _ = world
    row = db.query("SELECT * FROM picks WHERE pick_date='2030-06-01' LIMIT 1")[0]
    assert row["p_fair"] == pytest.approx(CANARY_FAIR) and row["edge_ev"] == pytest.approx(CANARY_EDGE)
    assert row["rank_score"] is not None and row["books"] and row["quality"] == "main"
    assert stats_mod.accuracy_report(db)["legs"]["decided"] == 4                 # still available to the CLI


@pytest.mark.parametrize("cmd", ["/top", "/daily", "/today", "/stats", "/yesterday"])
def test_typed_commands_are_clean(world, cmd):
    fetcher, db, cfg, _ = world
    tg = FakeTG()
    assert handle_command(cmd, "42", service=fetcher, tg=tg, cfg=cfg, db=db)
    assert tg.sends, f"{cmd} sent nothing"
    assert_clean(_seen(tg), cmd)


def test_start_and_the_menu_are_clean(world):
    _, db, cfg, _ = world
    tg = FakeTG()
    handle_start(db, tg, "42", {"first_name": "Ada", "username": "ada"})
    handle_callback({"id": "m", "data": "menu", "from": {"first_name": "Ada"},
                     "message": {"message_id": 9, "chat": {"id": "42"}}}, service=None, tg=tg, cfg=cfg, db=db)
    assert_clean(_seen(tg) + callbacks.commands_text() + callbacks.welcome_message("Ada"), "/start and menu")


@pytest.mark.parametrize("mode", ["new", "edit_pages", "edit"])
def test_every_view_and_page_button_is_clean(world, mode):
    """Pagination, 'View All Predictions', the stats / yesterday views, in every navigation mode."""
    fetcher, db, _, snap = world
    cfg = Config(timezone=TZ, view_navigation=mode)
    tg = FakeTG()
    day = snap.date.replace("-", "")
    presses = [pagination.encode_callback(v, p, day if v in (VIEW_TOP, VIEW_DAILY) else None)
               for v in (VIEW_TOP, VIEW_DAILY, VIEW_STATS, VIEW_YESTERDAY) for p in (0, 1, 2)]
    for data in presses:
        handle_callback({"id": "c", "data": data, "message": {"message_id": 5, "chat": {"id": "42"}}},
                        service=fetcher, tg=tg, cfg=cfg, db=db, page_size=3)
    assert len(tg.sends) + len(tg.edits) >= len(presses) - 3
    assert_clean(_seen(tg), f"callbacks ({mode})")


def test_every_rendered_page_of_every_view_is_clean(world):
    _, db, _, snap = world
    for view in (VIEW_TOP, VIEW_DAILY, VIEW_STATS, VIEW_YESTERDAY):
        for page in range(4):
            text, markup = pagination.render_view(snap, view, page, 3, TZ, db=db)
            assert_clean(text + json.dumps(markup), f"{view} page {page}")


def test_the_scheduled_broadcast_cards_are_clean(world):
    _, db, cfg, snap = world
    for kind, text, markup in broadcast.picks_messages(snap, TZ):
        assert_clean(text + json.dumps(markup), f"broadcast {kind}")
    assert_clean(broadcast.picks_text(snap, TZ), "dry-run / channel text")


def test_delivery_to_subscribers_is_clean(world):
    _, db, _, snap = world
    db.subscriber_add("42")
    db.subscriber_add("77", timezone="America/New_York")
    tg = FakeTG()
    sent = broadcast.deliver_picks(db, Config(timezone="UTC"), tg, snap,
                                   now=datetime(2030, 6, 1, 9, 30, tzinfo=timezone.utc))
    assert sent == 2 and len(tg.sends) == 4                              # two cards each
    assert_clean(_seen(tg), "scheduled delivery to subscribers")


def test_the_recap_and_stats_cards_are_clean(world):
    _, db, cfg, _ = world
    raw = stats_mod.compute(db, datetime(2030, 6, 1, 8, 0))
    assert_clean(fmt.stats_message(raw), "stats card")
    assert_clean(fmt.recap_message(raw), "recap card")
    text, _ = pagination.render_stats(DailySnapshot(date=""), db=db, tz=TZ)
    assert_clean(text, "render_stats")


def test_stats_stays_simple_and_never_shows_calibration():
    raw = {"today": "2030-06-01", "settled_ever": 8, "last7": {"won": 6, "lost": 2, "roi": 12.5, "n": 8, "push": 0,
           "void": 0, "profit": 1.0, "best_win": None, "hit_rate": 75.0, "streak": "W2", "strip": [], "window_days": 7},
           "last30": {"won": 6, "lost": 2, "n": 8, "roi": 12.5, "push": 0, "void": 0, "profit": 1.0, "best_win": None},
           "all": {"won": 6, "lost": 2, "n": 8, "roi": 12.5, "push": 0, "void": 0, "profit": 1.0, "best_win": None},
           "yesterday": {}, "yesterday_top": {"won": 0, "lost": 0, "n": 0, "roi": 0.0, "push": 0, "void": 0,
           "profit": 0, "best_win": None}, "by_sport_yesterday": [], "top_ticket_7d": {"hit": 0, "days": 0},
           "top_all": {"won": 0, "lost": 0, "n": 0, "roi": 0.0, "push": 0, "void": 0, "profit": 0, "best_win": None},
           "streak": "W2", "brier": 0.31337}
    text = fmt.stats_message(raw)
    assert "75%" in text or "6" in text                                   # the normal win / hit-rate information
    assert_clean(text, "stats message with a Brier score available")


# --------------------------------------------------------------------------------------- admin stays admin
def test_lastrun_is_admin_only(world):
    """/lastrun is the one place the engine's diagnostics are shown; it must refuse everyone else."""
    from predbot.bot.commands import CommandLoop
    _, db, cfg, _ = world
    loop = CommandLoop(FakeTG(), db, cfg, {}, service=None)
    assert loop.handle("/lastrun", "12345", "12345") == "Not allowed."
    assert loop.handle("/lastrun", "999", "999") != "Not allowed."          # the configured admin still gets it


def test_the_analytics_remain_available_through_the_cli_report(world):
    _, db, _, _ = world
    text = stats_mod.accuracy_text(stats_mod.accuracy_report(db))
    assert "PREDICTION ACCURACY" in text and "by quality" in text        # internal, server-side: not removed


# --------------------------------------------------------------------------------------- structural guards
INTERNAL_FIELDS = {"p_fair", "fair_books", "edge_ev", "rank_score", "quality", "se", "p_lcb", "independent", "books",
                   "closing_price", "clv", "providers", "evidence"}


def test_the_display_record_cannot_carry_the_new_internal_fields():
    assert not INTERNAL_FIELDS & set(PickRecord.__dataclass_fields__)


def test_a_pick_card_is_built_from_a_fixed_whitelist():
    """Adding a diagnostic to the card model would have to be a deliberate edit of this list."""
    assert set(fmt.PickLine._fields) == {"sport", "match", "outcome", "league", "confidence", "when", "price", "country",
                                         "market_type", "state", "start", "sport_key"}


def test_the_cached_snapshot_payload_has_no_new_internal_pick_fields(world):
    _, _, _, snap = world
    payload = snap.to_dict()
    for tier in ("top_picks", "daily_picks"):
        for pick in payload[tier]:
            assert not INTERNAL_FIELDS & set(pick)
    assert "canarybook" not in json.dumps(payload["top_picks"] + payload["daily_picks"])


def test_a_card_shows_only_the_existing_fields(world):
    """The one number that is shown is the existing confidence (the calibrated probability), exactly as before."""
    _, _, _, snap = world
    pick = snap.daily_picks[0]
    block = "\n".join(fmt.pick_lines(fmt.pick_line_from_record(pick, TZ)))
    assert f"{round(pick.prob * 100)}%" in block and f"{pick.price:.2f}" in block
    assert block.count("📊") == 1 and block.count("💰") == 1 and block.count("🎯") == 1
    assert_clean(block, "a single pick block")
