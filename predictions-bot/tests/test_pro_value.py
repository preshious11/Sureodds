"""Pro value picks (shadow only). All inputs are SYNTHETIC unit inputs - no real prices, no network, no database
outside a temporary directory."""
import json
import os

import pytest

from predbot.config import Config
from predbot.model.devig import shin_devig
from predbot.pro_value import closing, shadow
from predbot.pro_value.credits import CreditGuard
from predbot.pro_value.fair import OUTCOMES, edge, fair_1x2_shin, primary_fair, sharp_books_1x2
from predbot.pro_value.matching import match_events, match_key, norm_team, same_team
from predbot.pro_value.oddsapi_raw import OddsApiClient, load_raw_dir, parse_1x2_events, save_raw, stamp
from predbot.pro_value.select import Gates, evaluate_match, select_pro
from predbot.pro_value.sporty1x2 import parse_sporty_1x2
from predbot.pro_value.store import ShadowStore

KICK = 1_800_000_000          # epoch seconds, arbitrary
NOW = KICK - 3 * 3600         # three hours before kickoff
RUN = 1_799_990_000


def sharp(home, draw, away):
    return {"Home": home, "Draw": draw, "Away": away}


def make_event(eid, home="Alpha", away="Beta", kickoff=KICK, fetched=RUN, books=None):
    if books is None:
        books = {"pinnacle": sharp(2.0, 3.5, 4.0)}
    return {"id": eid, "sport_key": "soccer_test", "home_team": home, "away_team": away,
            "commence_ts": kickoff, "bookmakers": books, "fetched_at": fetched}


def make_sporty(home="Alpha", away="Beta", kickoff=KICK, fetched=RUN, odds=None):
    return {"event_id": f"sr:{home}{away}", "home_team": home, "away_team": away, "kickoff_ts": kickoff,
            "odds": odds or sharp(2.2, 3.4, 4.2), "fetched_at": fetched}


def gates(**kw):
    base = dict(edge_min=0.02, odds_min=1.5, odds_max=4.0, review_edge=0.06, second_book_edge=0.02,
                max_price_age_s=30 * 60, top_n=3, near_miss_min=0.0)
    base.update(kw)
    return Gates(**base)


def price_for(fair_prob, target_edge):
    """A SportyBet price that gives exactly ``target_edge`` against ``fair_prob``."""
    return round((1.0 + target_edge) / fair_prob, 4)


FAIR_HOME = 0.4867   # Shin fair home for pinnacle 2.0 / 3.5 / 4.0 (see test_shin...)


def matched(odds_event, sporty_event):
    return {"odds": odds_event, "sporty": sporty_event}


# ---------------------------------------------------------------- edge
def test_edge_is_sporty_price_times_fair_minus_one():
    assert edge(2.0, 0.5) == pytest.approx(0.0)
    assert edge(2.2, 0.5) == pytest.approx(0.10)
    assert edge(1.8, 0.5) == pytest.approx(-0.10)


# ---------------------------------------------------------------- Shin
def test_shin_fair_probabilities_sum_to_one_and_match_the_devig_module():
    prices = sharp(2.91, 3.69, 2.39)
    fair = fair_1x2_shin(prices)
    assert sum(fair.values()) == pytest.approx(1.0, abs=1e-9)
    assert fair == pytest.approx(shin_devig(prices), abs=1e-12)


def test_shin_rejects_bad_inputs():
    with pytest.raises(ValueError):
        fair_1x2_shin({"Home": 2.0, "Draw": 3.5})            # missing outcome
    with pytest.raises(ValueError):
        fair_1x2_shin(sharp(2.0, 0.9, 4.0))                  # price not above 1.0


def test_primary_fair_follows_pinnacle_then_matchbook_then_betfair():
    books = {"matchbook": sharp(2.0, 3.5, 4.0), "betfair_ex_eu": sharp(2.1, 3.4, 3.9)}
    fairs = sharp_books_1x2(books)
    assert primary_fair(fairs)[0] == "matchbook"
    fairs = sharp_books_1x2({**books, "pinnacle": sharp(2.0, 3.6, 4.0)})
    assert primary_fair(fairs)[0] == "pinnacle"
    assert primary_fair(sharp_books_1x2({"draftkings": sharp(2.0, 3.5, 4.0)})) is None


# ---------------------------------------------------------------- matching
def test_team_names_normalise_and_match():
    assert same_team("Newcastle United", "Newcastle")
    assert same_team("Brighton and Hove Albion", "Brighton")
    assert same_team("AZ Alkmaar", "Alkmaar")
    assert not same_team("Arsenal", "Chelsea")
    assert norm_team("FC Porto") == "porto"


def test_kickoff_tolerance_is_sixty_minutes():
    oe = make_event("1", home="Alpha FC", away="Beta FC", kickoff=KICK)
    within = make_sporty("Alpha", "Beta", kickoff=KICK + 59 * 60)
    outside = make_sporty("Alpha", "Beta", kickoff=KICK + 61 * 60)
    assert len(match_events([oe], [within], window_min=60)) == 1
    assert match_events([oe], [outside], window_min=60) == []


def test_ambiguous_matches_are_not_guessed():
    oe = make_event("1")
    two = [make_sporty("Alpha", "Beta", kickoff=KICK), make_sporty("Alpha", "Beta", kickoff=KICK + 600)]
    assert match_events([oe], two, window_min=60) == []


# ---------------------------------------------------------------- gates
def test_edge_at_or_above_minimum_is_selected_and_below_is_near_miss():
    sel = evaluate_match(matched(make_event("1"), make_sporty(odds=sharp(price_for(FAIR_HOME, 0.03), 3.4, 4.2))), gates())
    home = next(r for r in sel if r["outcome"] == "Home")
    assert home["status"] == "selected" and home["edge"] == pytest.approx(0.03, abs=1e-3)
    near = evaluate_match(matched(make_event("2"), make_sporty(odds=sharp(price_for(FAIR_HOME, 0.01), 3.4, 4.2))), gates())
    h = next(r for r in near if r["outcome"] == "Home")
    assert h["status"] == "near_miss" and 0.0 <= h["edge"] < 0.02


def test_negative_edge_is_rejected_not_stored_as_near_miss():
    rows = evaluate_match(matched(make_event("1"), make_sporty(odds=sharp(1.6, 3.2, 4.8))), gates())
    h = next(r for r in rows if r["outcome"] == "Home")
    assert h["status"] == "rejected" and h["reason"] == "edge_negative"


@pytest.mark.parametrize("price,ok", [(1.49, False), (1.5, True), (4.0, True), (4.01, False)])
def test_odds_band_boundaries(price, ok):
    odds = sharp(price, 3.4, 4.2)
    rows = evaluate_match(matched(make_event("1"), make_sporty(odds=odds)), gates())
    h = next(r for r in rows if r["outcome"] == "Home")
    assert (h["reason"] != "odds_out_of_band") == ok


def test_prices_more_than_thirty_minutes_apart_are_stale():
    rows = evaluate_match(matched(make_event("1", fetched=RUN), make_sporty(fetched=RUN + 31 * 60)), gates())
    home = next(r for r in rows if r["outcome"] == "Home")
    assert home["reason"] == "stale_prices"
    rows = evaluate_match(matched(make_event("1", fetched=RUN), make_sporty(fetched=RUN + 29 * 60)), gates())
    assert all(r["reason"] != "stale_prices" for r in rows)


def test_edge_above_review_threshold_needs_a_second_sharp_book():
    # Pinnacle says home is much shorter than sporty -> edge well above 6%
    pin_only = {"pinnacle": sharp(2.4, 3.5, 3.0)}
    rows = evaluate_match(matched(make_event("1", books=pin_only), make_sporty(odds=sharp(3.0, 3.4, 4.2))), gates())
    h = next(r for r in rows if r["outcome"] == "Home")
    assert h["edge"] > 0.06
    assert h["status"] == "needs_review" and h["reason"] == "no_second_sharp_book"


def test_second_book_must_confirm_the_same_outcome():
    strong = {"pinnacle": sharp(2.4, 3.5, 3.0), "matchbook": sharp(2.4, 3.5, 3.0)}
    rows = evaluate_match(matched(make_event("1", books=strong), make_sporty(odds=sharp(3.0, 3.4, 4.2))), gates())
    h = next(r for r in rows if r["outcome"] == "Home")
    assert h["status"] == "selected" and h["second_book"] == "matchbook" and h["second_edge"] >= 0.02
    weak = {"pinnacle": sharp(2.4, 3.5, 3.0), "matchbook": sharp(3.6, 3.5, 2.2)}
    rows = evaluate_match(matched(make_event("1", books=weak), make_sporty(odds=sharp(3.0, 3.4, 4.2))), gates())
    h = next(r for r in rows if r["outcome"] == "Home")
    assert h["status"] == "needs_review" and h["reason"] == "second_book_edge_low"


def test_no_sharp_price_means_no_candidate_at_all():
    rows = evaluate_match(matched(make_event("1", books={"draftkings": sharp(2.0, 3.5, 4.0)}), make_sporty()), gates())
    assert rows == []


def test_one_pick_per_match_is_its_best_outcome():
    odds = sharp(2.2, 4.6, 3.1)      # two outcomes can clear the gates on one match
    m = matched(make_event("1", books={"pinnacle": sharp(2.0, 3.5, 4.0)}), make_sporty(odds=odds))
    sel = select_pro([m], gates())
    assert len(sel.picks) <= 1
    if sel.picks:
        assert sel.picks[0]["edge"] == max(r["edge"] for r in evaluate_match(m, gates()) if r["status"] == "selected")


def test_top_n_caps_the_day_at_three_and_ranks_by_edge():
    ms = []
    for i in range(5):
        target = 0.025 + 0.005 * i                       # five distinct edges, all inside the selectable band
        ms.append(matched(make_event(str(i), home=f"H{i}", away=f"A{i}", kickoff=KICK + i),
                          make_sporty(home=f"H{i}", away=f"A{i}", kickoff=KICK + i,
                                      odds=sharp(price_for(FAIR_HOME, target), 3.4, 4.2))))
    sel = select_pro(ms, gates(top_n=3))
    assert len(sel.picks) == 3
    edges = [p["edge"] for p in sel.picks]
    assert edges == sorted(edges, reverse=True)
    assert [p["rank"] for p in sel.picks] == [1, 2, 3]
    assert sel.picks[0]["home"] == "H4"                  # highest edge ranks first


def test_no_passing_candidate_records_no_pro_pick_today():
    sel = select_pro([matched(make_event("1"), make_sporty(odds=sharp(1.6, 3.2, 4.8)))], gates())
    assert sel.picks == []
    assert sel.no_pick_reason == "no Pro pick today"


# ---------------------------------------------------------------- Bet of the Day exclusion (shadow only)
def test_botd_loses_matches_already_taken_by_pro_and_gets_the_next_candidate(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    botd = [{"match_key": "a"}, {"match_key": "b"}, {"match_key": "c"}]
    report = shadow._botd_comparison(botd, {"a"}, store, "r1")
    assert report == {"candidates": 3, "lost_to_pro": 1, "substitutes": ["b"]}
    rows = store.conn.execute("SELECT botd_match_key, lost_to_pro, substitute_match_key FROM pro_value_botd_compare "
                              "ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("a", 1, "b"), ("b", 0, None), ("c", 0, None)]
    store.close()


# ---------------------------------------------------------------- credits
def test_guard_refuses_when_the_balance_is_unknown():
    assert CreditGuard(reserve=80, cap=5).can_spend(1) is False


def test_guard_keeps_the_reserve_floor():
    g = CreditGuard(reserve=80, cap=5, remaining=81)
    assert g.can_spend(1) is True            # 81 - 1 = 80 is exactly the floor
    g.observe("80")
    assert g.can_spend(1) is False           # 80 - 1 = 79 would breach the reserve


def test_guard_enforces_the_daily_cap():
    g = CreditGuard(reserve=80, cap=2, remaining=500)
    g.spend(1)
    g.spend(1)
    assert g.can_spend(1) is False
    with pytest.raises(RuntimeError):
        g.spend(1)


def test_observe_ignores_non_numeric_headers():
    g = CreditGuard(reserve=80, cap=5, remaining=300)
    g.observe("not-a-number")
    g.observe(None)
    assert g.remaining == 300


# ---------------------------------------------------------------- closing capture
class FakeClient:
    """Stands in for OddsApiClient: spends from the same guard the real client uses."""

    def __init__(self, events_by_league, guard=None):
        self.events_by_league = events_by_league
        self.guard = guard
        self.calls = []

    def fetch_league_h2h(self, league, now_ts):
        if self.guard is not None:
            if not self.guard.can_spend(1):
                return None
            self.guard.spend(1)
        self.calls.append(league)
        return self.events_by_league.get(league, [])


def _pick_row(store, league, kick, home, away, odds, outcome="Home"):
    key = match_key(home, away, kick)
    store.insert_rows("run", RUN, [{"match_key": key, "league": league, "home": home, "away": away,
                                    "kickoff_ts": kick, "outcome": outcome, "sporty_odds": odds,
                                    "sporty_fetched_at": RUN, "fair_probability": 0.5, "fair_book": "pinnacle",
                                    "sharp_fetched_at": RUN, "edge": 0.1, "status": "selected", "rank": 1}])


def test_picks_in_one_league_and_kickoff_hour_share_one_call(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    _pick_row(store, "soccer_a", KICK, "Alpha", "Beta", 2.2)
    _pick_row(store, "soccer_a", KICK + 300, "Gamma", "Delta", 2.5)     # same league, same hour bucket, 5 min later
    _pick_row(store, "soccer_b", KICK, "Eps", "Zeta", 2.1)
    now = KICK - 15 * 60
    groups = closing.due_groups(store.open_pending_picks(now), now, window_min=20)
    assert sorted(len(v) for v in groups.values()) == [1, 2]
    store.close()


def test_nothing_is_captured_outside_the_window_or_after_kickoff(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    _pick_row(store, "soccer_a", KICK, "Alpha", "Beta", 2.2)
    early = KICK - 60 * 60
    assert closing.due_groups(store.open_pending_picks(early), early, window_min=20) == {}
    late = KICK + 60
    assert closing.due_groups(store.open_pending_picks(late), late, window_min=20) == {}
    store.close()


def test_closing_clv_uses_the_closing_fair_price(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    _pick_row(store, "soccer_a", KICK, "Alpha", "Beta", 2.2, outcome="Home")
    now = KICK - 10 * 60
    ev = [make_event("1", home="Alpha", away="Beta", kickoff=KICK, books={"pinnacle": sharp(2.0, 3.5, 4.0)})]
    guard = CreditGuard(reserve=80, cap=5, remaining=300)
    totals = closing.run_closing(FakeClient({"soccer_a": ev}), store, now, 20, guard)
    assert totals["captured"] == 1 and totals["calls"] == 1
    row = store.conn.execute("SELECT * FROM pro_value_rows").fetchone()
    fair_home = fair_1x2_shin(sharp(2.0, 3.5, 4.0))["Home"]
    assert row["closing_fair_probability"] == pytest.approx(fair_home)
    assert row["clv"] == pytest.approx(2.2 * fair_home - 1.0)
    store.close()


def test_daily_closing_cap_stops_further_league_calls(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    guard = CreditGuard(reserve=80, cap=2, remaining=300)
    for i, league in enumerate(["soccer_a", "soccer_b", "soccer_c"]):
        _pick_row(store, league, KICK + i * 60, f"H{i}", f"A{i}", 2.2)     # three leagues, all due now
    now = KICK - 10 * 60
    client = FakeClient({}, guard=guard)
    totals = closing.run_closing(client, store, now, 20, guard)
    assert len(client.calls) == 2 and totals["refused"] == 1 and guard.spent_today == 2
    store.close()


def test_reserve_guard_blocks_closing_calls_entirely(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    _pick_row(store, "soccer_a", KICK, "Alpha", "Beta", 2.2)
    client = FakeClient({})
    totals = closing.run_closing(client, store, KICK - 600, 20, CreditGuard(reserve=80, cap=5, remaining=80))
    assert client.calls == [] and totals["refused"] == 1
    store.close()


# ---------------------------------------------------------------- raw save before parse
def test_raw_response_is_written_to_disk_before_it_is_parsed(tmp_path, monkeypatch):
    body = [{"id": "e1", "sport_key": "soccer_a", "home_team": "Alpha", "away_team": "Beta",
             "commence_time": "2030-01-01T12:00:00Z", "bookmakers": []}]

    class Resp:
        status_code = 200
        content = json.dumps(body).encode()
        headers = {"x-requests-remaining": "300"}

    class Session:
        def get(self, url, params, timeout):
            return Resp()

    seen = {}
    import predbot.pro_value.oddsapi_raw as raw

    real_parse = raw.parse_1x2_events

    def checking_parse(body_, league, fetched):
        seen["files"] = os.listdir(tmp_path)           # the file must already exist when parsing runs
        return real_parse(body_, league, fetched)

    monkeypatch.setattr(raw, "parse_1x2_events", checking_parse)
    client = OddsApiClient("k", str(tmp_path), CreditGuard(reserve=80, cap=5, remaining=300), session=Session())
    events = client.fetch_league_h2h("soccer_a", KICK)
    assert len(seen["files"]) == 1 and seen["files"][0].startswith("soccer_a__")
    assert events and events[0]["home_team"] == "Alpha"


def test_saving_never_overwrites_an_existing_raw_file(tmp_path):
    save_raw(str(tmp_path), "soccer_a", b"[]", KICK)
    with pytest.raises(FileExistsError):
        save_raw(str(tmp_path), "soccer_a", b"[]", KICK)
    assert stamp(KICK).endswith("Z")


def test_load_raw_dir_round_trips_saved_files(tmp_path):
    body = [{"id": "e1", "sport_key": "soccer_a", "home_team": "Alpha", "away_team": "Beta",
             "commence_time": "2030-01-01T12:00:00Z",
             "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                 {"name": "Alpha", "price": 2.0}, {"name": "Draw", "price": 3.5}, {"name": "Beta", "price": 4.0}]}]}]}]
    save_raw(str(tmp_path), "soccer_a", json.dumps(body).encode(), KICK)
    events = load_raw_dir(str(tmp_path))
    assert len(events) == 1 and events[0]["bookmakers"]["pinnacle"] == {"Home": 2.0, "Draw": 3.5, "Away": 4.0}


# ---------------------------------------------------------------- SportyBet parsing (synthetic catalogue)
def test_sporty_parse_keeps_only_events_with_a_full_1x2_market():
    data = {"tournaments": [{"events": [
        {"eventId": "sr:match:1", "homeTeamName": "Alpha", "awayTeamName": "Beta", "estimateStartTime": KICK * 1000,
         "markets": [{"id": "1", "outcomes": [{"desc": "Home", "odds": "2.2"}, {"desc": "Draw", "odds": "3.4"},
                                             {"desc": "Away", "odds": "4.2"}]}]},
        {"eventId": "sr:match:2", "homeTeamName": "Gamma", "awayTeamName": "Delta", "estimateStartTime": KICK * 1000,
         "markets": [{"id": "18", "outcomes": []}]}]}]}
    out = parse_sporty_1x2(data)
    assert len(out) == 1 and out[0]["odds"] == {"Home": 2.2, "Draw": 3.4, "Away": 4.2}


# ---------------------------------------------------------------- safety
def test_shadow_store_refuses_the_production_or_bot_database(tmp_path):
    with pytest.raises(ValueError):
        ShadowStore(str(tmp_path / "predbot.db"))
    with pytest.raises(ValueError):
        ShadowStore(os.path.join("data", "predbot.db"))


def test_shadow_run_is_inert_while_disabled(tmp_path):
    cfg = Config()
    assert cfg.pro_value_shadow_enabled is False
    store = ShadowStore(str(tmp_path / "shadow.db"))
    assert shadow.run_shadow(cfg, raw_dir=str(tmp_path), sporty_events=[], store=store, now_ts=NOW) == {"status": "disabled"}
    assert store.conn.execute("SELECT COUNT(*) FROM pro_value_rows").fetchone()[0] == 0
    store.close()


def test_pro_value_defaults_match_the_specification():
    cfg = Config()
    assert cfg.pro_value_credit_cap == 5
    assert cfg.pro_value_edge_min == pytest.approx(0.02)
    assert (cfg.pro_value_odds_min, cfg.pro_value_odds_max) == (1.5, 4.0)
    assert cfg.pro_value_review_edge == pytest.approx(0.06)
    assert cfg.pro_value_max_price_age_min == 30
    assert cfg.pro_value_top_n == 3
    assert cfg.pro_value_raw_dir == ""
