"""Historical odds are linked to existing results, never inserted, never fuzzy-guessed, and never reach a model."""
from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from predbot.backtest.football import BTMatch, Prediction, disagreement_bins, from_history, walk_forward
from predbot.backtest.synthetic import synthetic_league
from predbot.history import odds_link
from predbot.history.store import HistoryStore
from predbot.model.football_strength import StrengthParams
from predbot.storage.identity import IdentityRegistry

CLUBS = ["Arsenal FC", "Chelsea FC", "Wolverhampton Wanderers FC", "Brighton & Hove Albion FC"]
CSV_NAMES = {"Arsenal FC": "Arsenal", "Chelsea FC": "Chelsea", "Wolverhampton Wanderers FC": "Wolves",
             "Brighton & Hove Albion FC": "Brighton"}
HEADER = "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,PSCH,PSCD,PSCA,B365CH,B365CD,B365CA,AvgCH,AvgCD,AvgCA,PC>2.5,PC<2.5,B365H,B365D,B365A"


def _fixtures():
    out, day = [], datetime(2025, 8, 16, 14, tzinfo=timezone.utc)
    for i, h in enumerate(CLUBS):
        for j, a in enumerate(CLUBS):
            if h != a:
                out.append((h, a, day, (i + j) % 4, (i * j) % 3))
                day += timedelta(days=3)
    return out


@pytest.fixture
def world(db):
    reg, store = IdentityRegistry(db), HistoryStore(db)
    ids = {c: reg.participant("football_data", "football", c, provider_id=f"fd:{c}") for c in CLUBS}
    for h, a, when, hg, ag in _fixtures():
        store.add_result(sport="football", competition_id="epl", kickoff=when, home_id=ids[h], away_id=ids[a],
                         home_goals=hg, away_goals=ag, source="football_data", source_match_id=f"{when:%Y%m%d}{h}")
    return db, store, ids


def _csv(rows=None, drop_first_odds=False):
    lines = [HEADER]
    for n, (h, a, when, hg, ag) in enumerate(rows or _fixtures()):
        odds = "2.0,3.4,3.8,2.1,3.3,3.7,2.05,3.35,3.75,1.9,1.95,2.2,3.5,3.6"
        if drop_first_odds and n == 0:
            odds = ",,,,,,,,,,,,,"
        lines.append(f"E0,{when:%d/%m/%Y},{when:%H:%M},{CSV_NAMES.get(h, h)},{CSV_NAMES.get(a, a)},{hg},{ag},{odds}")
    return "\n".join(lines) + "\n"


def _counts(db):
    return (db.query("SELECT COUNT(*) n FROM match_results")[0]["n"], db.query("SELECT COUNT(*) n FROM canonical_participants")[0]["n"])


def test_every_fixture_links_to_an_existing_match_and_nothing_is_inserted(world):
    db, store, ids = world
    before = _counts(db)
    rep = odds_link.link_csv(db, _csv(), "epl", "2025")
    assert rep["counts"]["linked"] == 12 and rep["counts"]["csv_matches"] == 12
    assert not rep["unresolved_names"] and not rep["ambiguous_names"]
    assert _counts(db) == before                                   # no match, no participant created from CSV spellings
    # the two short names were learned from fixtures and are reported, pointing at the right clubs
    assert rep["learned_aliases"] == {"Wolves": ids["Wolverhampton Wanderers FC"], "Brighton": ids["Brighton & Hove Albion FC"]}
    first = store.results(competition_id="epl")[0]
    quotes = store.odds_ext(first.id)
    assert quotes[("pinnacle", "1x2", "close")] == (2.0, 3.4, 3.8) and quotes[("pinnacle", "ou25", "close")][:2] == (1.9, 1.95)
    assert quotes[("bet365", "1x2", "pre")] == (2.0, 3.4, 3.8) or ("bet365", "1x2", "pre") in quotes
    assert ("pinnacle", "1x2", "pre") not in quotes                # columns the file lacks are never invented


def test_linking_is_idempotent_and_never_overwrites_a_stored_quote(world):
    db, store, ids = world
    odds_link.link_csv(db, _csv(), "epl", "2025")
    n = db.query("SELECT COUNT(*) n FROM match_odds_ext")[0]["n"]
    again = odds_link.link_csv(db, _csv().replace("2.0,3.4,3.8", "9.0,9.0,9.0"), "epl", "2025")
    assert db.query("SELECT COUNT(*) n FROM match_odds_ext")[0]["n"] == n and again["counts"]["odds_rows_written"] == 0
    assert store.odds_ext(store.results(competition_id="epl")[0].id)[("pinnacle", "1x2", "close")] == (2.0, 3.4, 3.8)


def test_a_score_that_disagrees_is_reported_not_linked(world):
    db, store, ids = world
    rows = _fixtures()
    rows[3] = rows[3][:3] + (rows[3][3] + 1, rows[3][4])
    rep = odds_link.link_csv(db, _csv(rows), "epl", "2025")
    assert rep["counts"]["score_mismatch"] == 1 and rep["counts"]["linked"] == 11
    assert len(db.query("SELECT DISTINCT match_id FROM match_odds_ext")) == 11


def test_an_unknown_fixture_is_unmatched_and_an_ambiguous_one_is_never_guessed(world):
    db, store, ids = world
    h, a, when, hg, ag = _fixtures()[0]
    # a second stored match of the same two clubs one day later makes the fixture ambiguous within the +/-1 day window
    store.add_result(sport="football", competition_id="epl", kickoff=when + timedelta(days=1), home_id=ids[h], away_id=ids[a],
                     home_goals=hg, away_goals=ag, source="football_data", source_match_id="twin")
    stray = (CLUBS[0], CLUBS[1], datetime(2030, 1, 1, tzinfo=timezone.utc), 1, 1)
    rep = odds_link.link_csv(db, _csv(_fixtures() + [stray]), "epl", "2025")
    assert rep["counts"]["ambiguous"] == 1 and rep["counts"]["unmatched"] == 1 and rep["counts"]["linked"] == 11


def test_a_name_with_too_little_fixture_evidence_stays_unresolved(world):
    db, store, ids = world
    first_five = _fixtures()[:5]                                    # Wolves appears twice, Brighton once: fewer than 3 votes
    rep = odds_link.link_csv(db, _csv(first_five), "epl", "2025")
    assert rep["learned_aliases"] == {} and set(rep["unresolved_names"]) == {"Wolves", "Brighton"}
    assert rep["counts"]["linked"] == 2 and rep["counts"]["unresolved_team"] == 3


def test_rows_without_odds_link_but_carry_no_quotes(world):
    db, store, ids = world
    rep = odds_link.link_csv(db, _csv(drop_first_odds=True), "epl", "2025")
    assert rep["counts"]["linked_without_odds"] == 1 and rep["counts"]["linked"] == 11


def test_column_audit_reports_what_each_file_actually_has(world):
    db, store, ids = world
    cols = odds_link.link_csv(db, _csv(), "epl", "2025")["columns"]
    assert cols["1x2"]["pinnacle:close"] == {"columns": ["PSCH", "PSCD", "PSCA"], "present": True, "rows": 12}
    assert cols["1x2"]["pinnacle:pre"]["present"] is False and cols["1x2"]["bet365:pre"]["rows"] == 12
    assert cols["ou25"]["pinnacle:close"]["rows"] == 12 and cols["ou25"]["market_avg:close"]["present"] is False


def test_download_is_ledgered_so_a_past_file_is_fetched_once(db, tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nE0,16/08/2024,A,B,1,0\n"

    plan = odds_link.plan_csv(db, ["E0"], ["2024"])
    assert plan == [("E0", "2024", "https://www.football-data.co.uk/mmz4281/2425/E0.csv")]
    rep = odds_link.download_csv(db, plan, tmp_path, get=get, sleep=lambda s: None)
    assert rep["E0:2024"]["rows"] == 1 and (tmp_path / "2024" / "E0.csv").exists()
    assert odds_link.plan_csv(db, ["E0"], ["2024"]) == [] and len(calls) == 1


def test_a_failed_download_is_recorded_and_backed_off(db, tmp_path):
    def boom(url):
        raise OSError("down")

    plan = odds_link.plan_csv(db, ["E0"], ["2024"])
    rep = odds_link.download_csv(db, plan, tmp_path, get=boom, sleep=lambda s: None)
    assert "OSError" in rep["E0:2024"]["error"] and odds_link.plan_csv(db, ["E0"], ["2024"]) == []


def test_from_history_uses_exactly_one_book_and_never_mixes(world):
    db, store, ids = world
    odds_link.link_csv(db, _csv(), "epl", "2025")
    pin = from_history(store, None, baseline=("pinnacle", "close"))
    pre = from_history(store, None, baseline=("pinnacle", "pre"))
    assert all(m.market is not None and m.ou_market is not None for m in pin)
    assert all(m.market is None for m in pre)                      # pinnacle has no 'pre' columns here: no fallback to another book
    assert all(abs(sum(m.market) - 1) < 1e-9 for m in pin)


def test_odds_never_influence_the_models_that_are_compared_with_them():
    matches, _ = synthetic_league(seed=11, teams=10, seasons=3)
    rng = random.Random(3)
    def fake_market():
        v = [rng.random() + 0.1 for _ in range(3)]
        return tuple(x / sum(v) for x in v)

    scrambled = [replace(m, market=fake_market(), ou_market=rng.random()) for m in matches]
    start = matches[len(matches) // 2].kickoff
    a = walk_forward(matches, StrengthParams(), score_from=start)
    b = walk_forward(scrambled, StrengthParams(), score_from=start)
    assert [p.p1x2["poisson"] for p in a] == [p.p1x2["poisson"] for p in b]
    assert [p.p1x2["elo"] for p in a] == [p.p1x2["elo"] for p in b]
    assert [p.p_over25["poisson"] for p in a] == [p.p_over25["poisson"] for p in b]


# ------------------------------------------------------------------------------------------------ disagreement
def _synthetic_predictions(n=1500, seed=5, who_is_right="market"):
    rng = random.Random(seed)
    out = []
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        w = [rng.random() + 0.3 for _ in range(3)]
        truth = tuple(x / sum(w) for x in w)
        noisy = [x * math.exp(rng.gauss(0, 0.35)) for x in truth]
        noisy = tuple(x / sum(noisy) for x in noisy)
        outcome = rng.choices(range(3), truth)[0]
        hg, ag = (1, 0) if outcome == 0 else (1, 1) if outcome == 1 else (0, 1)
        model, market = (noisy, truth) if who_is_right == "market" else (truth, noisy)
        out.append(Prediction(BTMatch(t0 + timedelta(hours=i), "x", f"h{i}", f"a{i}", hg, ag), {"poisson": model, "market": market}))
    return out


def test_when_the_market_is_right_disagreement_is_noise_and_the_market_is_closer():
    d = disagreement_bins(_synthetic_predictions(who_is_right="market"))
    slope = d["slope_outcome_minus_market_on_delta"]
    assert slope["ci95"][1] < 0.5
    big = [b for b in d["bins"] if b["gap_pp"] in ("10-15", "15-100")]
    assert big and all(b["closer_by_brier"] == "market" for b in big)
    assert sum(b["n"] for b in d["bins"]) == d["n_instances"] == 3 * d["n_matches"]


def test_when_the_model_is_right_disagreement_is_signal():
    d = disagreement_bins(_synthetic_predictions(who_is_right="model"))
    assert d["slope_outcome_minus_market_on_delta"]["ci95"][0] > 0.5
    assert all(b["closer_by_brier"] == "poisson" for b in d["bins"] if b["gap_pp"] in ("10-15", "15-100"))


def test_identical_models_have_no_bullish_or_bearish_cells():
    preds = _synthetic_predictions()
    for p in preds:
        p.p1x2["poisson"] = p.p1x2["market"]
    d = disagreement_bins(preds)
    assert d["cells"] == [] and [b["gap_pp"] for b in d["bins"]] == ["0-2"]
