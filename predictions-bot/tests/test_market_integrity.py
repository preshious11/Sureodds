import json

import pytest

from predbot.bot.format import format_outcome
from predbot.cache.providers import decode_events, encode_events
from predbot.config import Config
from predbot.custom_picks import normalize
from predbot.jobs.daily_picks import prediction_row
from predbot.markets.contracts import market_contract_reason
from predbot.model.candidates import capture_candidate
from predbot.model.engine import representative_predictions
from predbot.providers.base import MarketLine
from predbot.providers.sharpapi import SharpApiProvider
from predbot.providers.sportsgameodds import SportsGameOddsProvider
from predbot.selection.daily import select_daily
from predbot.selection.top import select_top
from tests.conftest import dt, load
from tests.test_picker import ev, sc
from predbot.util.timeutil import iso


@pytest.mark.parametrize(
    "sport,market,selection,period,stat,line,reason",
    [
        ("football", "dc", "home_draw", "full", "points", None, None),
        ("american_football", "dc", "home_draw", "1h", "points", None, "unsupported_market_for_sport"),
        ("baseball", "dc", "home_draw", "1h", "points", None, "unsupported_market_for_sport"),
        ("basketball", "result2", "home", "full", "points", None, None),
        ("baseball", "result2", "away", "full", "points", None, None),
        ("baseball", "total", "over", "1i5", "points", 4.5, None),
        ("tennis", "result2", "draw", "full", "points", None, "invalid_outcome"),
        ("baseball", "prop", "over", "full", "hits", 1.5, "invalid_entity"),
        ("basketball", "correct_score", "1-0", "full", "points", None, "unsupported_market_for_sport"),
        ("unknown", "result2", "home", "full", "points", None, "unsupported_sport"),
        ("football", "dc", "home_draw", "1h", "points", None, None),
        ("football", "dc", "home_draw", "full", "points", None, None),
    ],
)
def test_sport_market_contract(sport, market, selection, period, stat, line, reason):
    assert market_contract_reason(sport, market, selection, period, stat, "all", line) == reason


def test_contract_accepts_a_scoped_supported_player_prop():
    assert market_contract_reason(
        "baseball", "prop", "over", "full", "hits", "Alec Bohm", 1.5,
    ) is None


def test_sharp_provider_rejects_cross_sport_double_chance_and_keeps_source_identifier(budget):
    provider = SharpApiProvider("key", budget, ["nfl", "mlb", "epl"])
    raw = {
        "id": "odd-1",
        "market_id": "provider-market-17",
        "odds_decimal": 1.8,
        "market_type": "double_chance",
        "selection_type": "home_draw",
        "market_segment": "1st_half",
    }
    assert provider._row_to_line(raw, "american_football") is None
    assert provider._row_to_line(raw, "baseball") is None
    assert provider.parse_counts["MARKET_CONTRACT_UNSUPPORTED_MARKET_FOR_SPORT"] == 2

    soccer = provider._row_to_line(raw, "football")
    assert soccer is not None
    assert (soccer.market, soccer.selection, soccer.period) == ("dc", "home_draw", "1h")
    assert soccer.provider_market_id == "provider-market-17"


def test_sgo_three_way_outcome_is_not_coerced_to_non_soccer_double_chance(budget):
    provider = SportsGameOddsProvider("key", budget, ["FOOTBALL", "BASEBALL"], [])
    raw = {
        "statID": "points",
        "periodID": "game",
        "betTypeID": "ml3way",
        "sideID": "home+draw",
        "statEntityID": "all",
        "bookOdds": -110,
        "marketName": "Three Way Moneyline",
    }
    assert provider._parse_odd("odd-1", raw, "american_football", {}) is None
    soccer = provider._parse_odd("odd-1", raw, "football", {})
    assert soccer is not None
    assert soccer.market == "dc"
    assert soccer.provider_market_id == "ml3way:points:game"


def test_sgo_real_fixture_keeps_full_and_first_half_scope_and_rejects_other_sports(budget):
    provider = SportsGameOddsProvider("key", budget, ["SOCCER"], [])
    event = load("sgo_event.json")
    full_raw = event["odds"]["points-home-reg-ml3way-home+draw"]
    first_half_raw = event["odds"]["points-home-1h-ml3way-home"]

    full = provider._parse_odd("full-odd", full_raw, "football", {})
    first_half = provider._parse_odd("half-odd", first_half_raw, "football", {})
    assert full is not None and full.period == "full"
    assert first_half is not None and first_half.period == "1h"
    assert provider._parse_odd("baseball-odd", full_raw, "baseball", {}) is None
    assert provider._parse_odd("basketball-odd", full_raw, "basketball", {}) is None


def test_real_sharp_baseball_fixture_retains_verified_moneyline_and_run_line(budget):
    provider = SharpApiProvider("key", budget, ["mlb"])
    rows = load("sharp_mlb_main.json")["data"]
    parsed = [(row, provider._row_to_line(row, "baseball")) for row in rows]
    moneyline = next((row, line) for row, line in parsed if row["market_type"] == "moneyline" and line)
    run_line = next((row, line) for row, line in parsed if row["market_type"] == "run_line" and line)
    assert moneyline[1].market == "result2"
    assert moneyline[1].provider_market_id == moneyline[0]["market_id"]
    assert run_line[1].market == "handicap"
    assert run_line[1].provider_market_id == run_line[0]["market_id"]


def test_provider_cache_candidate_and_archive_round_trip_keep_market_provenance(db):
    event = ev(90, sport="baseball")
    event.canonical_event_id = "baseball-event-90"
    event.markets = [MarketLine("result2", "home", 1.8, provider_market_id="1_86363877")]
    decoded = decode_events(encode_events([event]))[0]
    assert decoded.markets[0].provider_market_id == "1_86363877"

    scored = sc(event, "result2", "home", .7, 1.8)
    scored.line.provider_market_id = "1_86363877"
    candidate = capture_candidate(scored)
    archived = prediction_row(scored, "2030-06-01")
    db.archive_ticket_candidate(archived)
    stored = db.ticket_candidate_rows("2030-06-01")[0]
    assert json.loads(stored["sources"])["_provider_market_id"] == "1_86363877"


def test_engine_rejects_invalid_market_without_dropping_valid_soccer_candidate():
    baseball = ev(91, sport="baseball")
    baseball.canonical_event_id = "baseball-event-91"
    soccer = ev(92, sport="football")
    soccer.canonical_event_id = "soccer-event-92"
    invalid = sc(baseball, "dc", "home_draw", .8, 1.5)
    valid = sc(soccer, "dc", "home_draw", .8, 1.5)

    result = representative_predictions([invalid, valid])
    assert len(result.ticket_predictions) == 1
    assert result.ticket_predictions[0].event.sport == "football"
    assert result.diagnostics["line_rejections"] == {"market_contract_unsupported_market_for_sport": 1}


def test_archived_selection_rejects_malformed_market_and_keeps_other_candidate():
    def row(identifier, sport, market, selection):
        return {
            "id": identifier, "canonical_event_id": identifier, "fixture_key": identifier,
            "status": "pending", "start_utc": iso(dt(hh=22)), "sport": sport, "market": market,
            "selection": selection, "period": "full", "stat": "points", "entity": "all",
            "line": None, "prob": .8, "price": 1.8, "ev": .2, "edge_ev": None,
            "quality": "main", "books": 5, "rank_score": .75, "sources": "{}",
        }

    valid = row("valid", "baseball", "result2", "home")
    invalid = row("invalid", "baseball", "dc", "home_draw")
    selected, diagnostics = select_top([invalid, valid], Config(), iso(dt()))
    assert [pick["id"] for pick in selected] == ["valid"]
    assert diagnostics["rejections"]["market_contract_unsupported_market_for_sport"] == 1


def test_valid_basketball_and_baseball_moneylines_reach_top_and_daily():
    rows = []
    for sport in ("basketball", "baseball"):
        event = ev(100 + len(rows), sport=sport)
        event.canonical_event_id = f"{sport}-valid-moneyline"
        scored = sc(event, "result2", "home", .8, 1.8)
        rows.append({
            "id": len(rows) + 1, "canonical_event_id": event.canonical_event_id,
            "fixture_key": event.canonical_event_id, "status": "pending",
            "start_utc": iso(dt(hh=22)), "sport": sport, "market": "result2",
            "league": sport, "canonical_competition_id": sport,
            "selection": "home", "period": "full", "stat": "points", "entity": "all",
            "line": None, "prob": scored.p_model, "price": scored.line.price, "ev": scored.ev,
            "edge_ev": None, "quality": "main", "books": 5, "rank_score": .75, "sources": "{}",
        })

    cfg = Config()
    for row in rows:
        top, _ = select_top([row], cfg, iso(dt()))
        assert top == [row]
    daily, _ = select_daily(rows, cfg, iso(dt()))
    assert {pick["sport"] for pick in daily} == {"basketball", "baseball"}


def test_custom_archive_and_telegram_render_reject_cross_sport_double_chance(caplog):
    row = {
        "prediction_date": "2030-06-01", "timezone": "Africa/Lagos",
        "canonical_event_id": "baseball-event-93", "sport": "baseball",
        "market": "dc", "selection": "home_draw", "period": "1h", "stat": "points",
        "entity": "all", "line": None, "price": 1.8, "p_model": .7,
        "start_utc": iso(dt(hh=22)), "generated_at": iso(dt(hh=12)),
        "home": "Home", "away": "Away", "market_name": "Double Chance",
        "provider_market_id": "provider-market-17",
    }
    assert normalize(row, "2030-06-01", "Africa/Lagos", dt(hh=14), dt(hh=12),
                     ["football", "baseball"]) is None
    assert "market_contract_unsupported_market_for_sport" in caplog.text

    pick = type("Pick", (), row)()
    assert format_outcome(pick) == ""
