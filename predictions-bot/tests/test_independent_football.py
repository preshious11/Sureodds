"""The independent football model: history-only ratings, coherent markets, p_model vs p_market, explicit fallbacks."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from predbot.config import Config
from predbot.model.calibration import Calibrator
from predbot.model.elo import Elo
from predbot.model.ensemble import EventModel
from predbot.model.football_strength import LeakageError, StrengthParams, fit_strength
from predbot.model.independent import FootballIndependentModel, IndependentModels
from predbot.providers.base import Event, MarketLine
from tests.history_helpers import COMP, football_event, load_history

AS_OF = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def world(db):
    ids, matches, store = load_history(db, seed=2, seasons=3, teams=12)
    as_of = max(m.kickoff for m in matches) + timedelta(days=3)
    return db, ids, matches, store, as_of


def _model(db, as_of, mode="shadow", **kw):
    reg = IndependentModels.default(db, Config(), as_of)
    return reg


def _scored(db, ids, as_of, home="T00", away="T01", mode="shadow", markets=None, league=None):
    event = football_event(home, away, as_of + timedelta(days=1), ids.get(home, ""), ids.get(away, ""), markets=markets)
    em = EventModel(event, Elo(db), Calibrator(db), independent=_model(db, as_of), independent_mode=mode)
    return em, {(s.line.market, s.line.selection, s.line.line): s for s in em.score_all()}


def test_ratings_recover_the_generating_strengths_from_results_only(world):
    db, ids, matches, store, as_of = world
    fit = fit_strength(store.results(competition_id=COMP), as_of, StrengthParams(half_life_days=10_000, prior_strength=2.0), COMP)
    assert fit is not None and fit.matches == len(matches)
    # a strong attack and weak defence separate the teams: the ratings are not all equal and have a home effect
    spread = max(fit.attack.values()) / min(fit.attack.values())
    assert spread > 1.3 and fit.mu_home > fit.mu_away
    assert abs(sum(fit.attack.values()) / len(fit.attack) - 1.0) < 1e-6


def test_fit_refuses_any_training_match_that_is_not_strictly_before_the_prediction_instant(world):
    db, ids, matches, store, as_of = world
    results = store.results(competition_id=COMP)
    with pytest.raises(LeakageError):
        fit_strength(results, results[-1].kickoff, StrengthParams())            # the last match IS the cut-off
    assert fit_strength(results, results[-1].kickoff + timedelta(seconds=1), StrengthParams()) is not None


def test_future_results_cannot_change_an_earlier_estimate(world):
    db, ids, matches, store, as_of = world
    cut = matches[len(matches) // 2].kickoff
    early = fit_strength(store.results(competition_id=COMP, before=cut), cut, StrengthParams(), COMP)
    # add a wild future result AFTER the cut: refitting as of the same cut must give the identical ratings
    reg_ids = list(ids.values())
    store.add_result(sport="football", competition_id=COMP, kickoff=cut + timedelta(days=30), home_id=reg_ids[0],
                     away_id=reg_ids[1], home_goals=9, away_goals=0, source="x", source_match_id="future")
    again = fit_strength(store.results(competition_id=COMP, before=cut), cut, StrengthParams(), COMP)
    assert early.attack == again.attack and early.defence == again.defence and early.mu_home == again.mu_home


def test_expected_goals_come_from_history_not_from_bookmaker_prices(world):
    db, ids, matches, store, as_of = world
    cheap = [MarketLine("result3", "home", 1.2, n_books=4, book_prices=[1.2] * 4), MarketLine("result3", "draw", 7.0, n_books=4, book_prices=[7.0] * 4),
             MarketLine("result3", "away", 15.0, n_books=4, book_prices=[15.0] * 4)]
    dear = [MarketLine("result3", "home", 9.0, n_books=4, book_prices=[9.0] * 4), MarketLine("result3", "draw", 4.0, n_books=4, book_prices=[4.0] * 4),
            MarketLine("result3", "away", 1.4, n_books=4, book_prices=[1.4] * 4)]
    a, b = _scored(db, ids, as_of, markets=cheap)[0], _scored(db, ids, as_of, markets=dear)[0]
    assert a.ind_estimate.lambdas == b.ind_estimate.lambdas                      # opposite markets, identical estimate
    pa = a.ind_estimate.price(MarketLine("result3", "home", 2.0))
    assert pa == b.ind_estimate.price(MarketLine("result3", "home", 2.0))


def test_markets_are_derived_coherently_from_one_score_distribution(world):
    db, ids, matches, store, as_of = world
    em, _ = _scored(db, ids, as_of)
    est = em.ind_estimate
    px = {s: est.price(MarketLine("result3", s, 2.0)) for s in ("home", "draw", "away")}
    assert sum(px.values()) == pytest.approx(1.0, abs=1e-6)
    dc = est.price(MarketLine("dc", "home_draw", 2.0))
    assert dc == pytest.approx(px["home"] + px["draw"], abs=1e-9)
    dnb = est.price(MarketLine("dnb", "home", 2.0))
    assert dnb == pytest.approx(px["home"] / (px["home"] + px["away"]), abs=1e-9)
    over, under = (est.price(MarketLine("total", s, 2.0, line=2.5)) for s in ("over", "under"))
    assert over + under == pytest.approx(1.0, abs=1e-6)
    yes, no = (est.price(MarketLine("btts", s, 2.0)) for s in ("yes", "no"))
    assert yes + no == pytest.approx(1.0, abs=1e-6) and 0 < yes < 1
    assert est.price(MarketLine("handicap", "home", 2.0, line=-0.5)) == pytest.approx(px["home"], abs=1e-6)


def test_shadow_mode_stores_p_independent_beside_an_unchanged_p_model(world):
    db, ids, matches, store, as_of = world
    _, off = _scored(db, ids, as_of, mode="off")
    _, shadow = _scored(db, ids, as_of, mode="shadow")
    key = ("result3", "home", None)
    assert shadow[key].p_model == off[key].p_model                              # production probability untouched
    s = shadow[key]
    assert s.p_independent is not None and s.independent_status == "ok" and s.p_model_basis == "market_blend"
    ind = s.notes["independent_model"]
    assert ind["p"] == pytest.approx(s.p_independent, abs=1e-4)
    assert ind["edge_vs_market"] == pytest.approx(s.p_independent - s.p_market, abs=1e-4)
    assert ind["ev"] == pytest.approx(s.p_independent * s.price - 1, abs=1e-4)
    assert s.p_market != s.p_independent                                       # two different numbers
    assert off[key].p_independent is None and off[key].notes["independent_model"]["status"] == "unavailable:not_configured"


def test_independent_mode_makes_p_model_the_independent_estimate_and_labels_the_fallback(world):
    db, ids, matches, store, as_of = world
    _, ind = _scored(db, ids, as_of, mode="independent")
    s = ind[("result3", "home", None)]
    assert s.p_model_basis == "independent" and s.p_model == pytest.approx(s.p_independent, abs=1e-9)
    # a market the model cannot price keeps the market-derived value and says so
    corners = [MarketLine("total", "over", 1.9, line=9.5, stat="corners", n_books=3, book_prices=[1.9] * 3),
               MarketLine("total", "under", 1.95, line=9.5, stat="corners", n_books=3, book_prices=[1.95] * 3)]
    _, fb = _scored(db, ids, as_of, mode="independent", markets=corners)
    c = fb[("total", "over", 9.5)]
    assert c.p_model_basis == "market_blend" and c.p_independent is None
    assert c.independent_status == "unavailable:unsupported_market"


@pytest.mark.parametrize("home,away,use_ids,expected", [
    ("T00", "ZZ Unknown", True, "unavailable:unknown_team"),
    ("T00", "T01", False, "unavailable:unresolved_participant"),
])
def test_an_unavailable_estimate_is_labelled_never_silently_replaced(world, home, away, use_ids, expected):
    db, ids, matches, store, as_of = world
    pid = {"T00": ids["T00"], "T01": ids["T01"], "ZZ Unknown": "football:participant:unseen"}
    event = football_event(home, away, as_of + timedelta(days=1), pid[home] if use_ids else "", pid[away] if use_ids else "")
    est, status = IndependentModels.default(db, Config(), as_of).estimate(event)
    assert est is None and status == expected


def test_no_history_and_other_sports_report_their_status(db):
    event = football_event("A", "B", AS_OF, "football:participant:a", "football:participant:b")
    assert IndependentModels.default(db, Config(), AS_OF).estimate(event)[1] == "unavailable:no_history"
    tennis = Event(provider="t", provider_event_id="1", sport="tennis", league="ATP", home="X", away="Y", start=AS_OF)
    assert IndependentModels.default(db, Config(), AS_OF).estimate(tennis) == (None, "unavailable:no_independent_model")


def test_independent_model_version_addition_was_reverted(db):
    """Section 7 of this phase's brief proposed an additive 'notes[\"independent_model_version\"]' field. It was
    implemented and tested (see git history for this change), then REVERTED after the full suite showed it broke
    tests/test_golden_decision_margins.py and tests/test_replay_golden.py, which assert an EXACT key set for
    sources._notes against a frozen golden slate - any new key there is a breaking change to those tests, not a
    safe additive one. Rather than rewrite golden fixtures for an optional, non-essential field (section 2's
    extended evaluation_report.py does not depend on it - it already distinguishes "no estimate" rows via
    independent_status/missing_data_rate without needing a separate version string), the addition was dropped.
    This test documents that decision so a future session does not reintroduce the same regression."""
    event = football_event("A", "B", AS_OF, "football:participant:a", "football:participant:b")
    em = EventModel(event, Elo(db), Calibrator(db), independent=IndependentModels.default(db, Config(), AS_OF),
                    independent_mode="shadow")
    s = em.score_all()[0]
    assert "independent_model_version" not in s.notes


def test_a_team_with_too_few_matches_is_insufficient_history_not_a_guess(world):
    db, ids, matches, store, as_of = world
    far = as_of + timedelta(days=4000)                      # ten years later: every match has decayed to nothing
    model = FootballIndependentModel(db, StrengthParams(half_life_days=30.0), far)
    event = football_event("T00", "T01", far + timedelta(days=1), ids["T00"], ids["T01"])
    assert model.estimate(event) == (None, "unavailable:insufficient_history")


def test_other_sports_are_unchanged_by_the_independent_layer(db):
    tennis_lines = [MarketLine("result2", "home", 1.7, n_books=3, book_prices=[1.7] * 3), MarketLine("result2", "away", 2.2, n_books=3, book_prices=[2.2] * 3)]
    e = Event(provider="t", provider_event_id="1", sport="tennis", league="ATP", home="X", away="Y", start=AS_OF, markets=tennis_lines)
    shadow = {s.line.selection: s for s in EventModel(e, Elo(db), Calibrator(db), independent=IndependentModels.default(db, Config(), AS_OF)).score_all()}
    off = {s.line.selection: s for s in EventModel(e, Elo(db), Calibrator(db)).score_all()}
    assert shadow["home"].p_model == off["home"].p_model
    assert shadow["home"].independent_status == "unavailable:no_independent_model" and shadow["home"].p_independent is None


def test_the_independent_estimate_survives_into_the_stored_prediction_row(world):
    """Stored notes must carry p_independent, edge, EV and status next to p_market (the Daily path also writes an
    unrelated list under 'independent', so the model's record lives under its own key)."""
    import json
    from predbot.jobs.daily_picks import store_picks
    from predbot.model.engine import representative_predictions
    from predbot.selection.evidence import assess
    from predbot.selection.picker import PickSet
    from predbot.util.fixtures import fixture_key
    db, ids, matches, store, as_of = world
    em, scored = _scored(db, ids, as_of, mode="shadow")
    lines = list(scored.values())
    for s in lines:
        s.event.canonical_event_id = fixture_key(s.event)
    result = representative_predictions(lines)
    for s in result.predictions:
        e = assess(s, Config(), {})
        s.quality, s.se, s.p_lcb, s.rank_score = e.quality, e.se, e.p_lcb, e.rank
        s.notes["independent"] = s.independent                      # exactly what build_picks does
    store_picks(PickSet(predictions=result.predictions), db, "2030-01-02")
    row = db.query("SELECT * FROM picks WHERE pick_date='2030-01-02' AND market='result3'")[0]
    notes = json.loads(row["sources"])["_notes"]
    assert notes["model_basis"] == "market_blend"
    model = notes["independent_model"]
    assert model["status"] == "ok" and model["p"] is not None and model["edge_vs_market"] is not None
    assert row["p_market"] is not None and abs(model["p"] - row["p_market"]) > 1e-6        # separate numbers, both stored
