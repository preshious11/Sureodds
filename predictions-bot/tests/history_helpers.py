"""Test helpers: a database holding a synthetic football history under a real canonical competition."""
from __future__ import annotations

from predbot.backtest.synthetic import synthetic_league
from predbot.history.store import HistoryStore
from predbot.providers.base import Event, MarketLine
from predbot.storage.identity import IdentityRegistry

COMP = "epl"
LEAGUE = "England - Premier League"


def load_history(db, seed=1, seasons=3, teams=12):
    """Store a synthetic league as results; returns ({name: participant id}, matches, store)."""
    matches, _ = synthetic_league(seed=seed, teams=teams, seasons=seasons, competition=COMP)
    reg, store = IdentityRegistry(db), HistoryStore(db)
    ids = {}
    for m in matches:
        for name in (m.home, m.away):
            if name not in ids:
                ids[name] = reg.participant("fixture-history", "football", name, provider_id=f"hist:{name}")
    rows = [dict(sport="football", competition_id=COMP, kickoff=m.kickoff, home_id=ids[m.home], away_id=ids[m.away],
                 home_goals=m.hg, away_goals=m.ag, source="synthetic", source_match_id=f"{m.kickoff.date()}:{m.home}:{m.away}")
            for m in matches]
    store.add_many(rows)
    return ids, matches, store


def football_event(home, away, start, home_id="", away_id="", league=LEAGUE, markets=None, provider="test"):
    e = Event(provider=provider, provider_event_id=f"{home}-{away}", sport="football", league=league, home=home,
              away=away, start=start, sources=[provider])
    e.markets = markets if markets is not None else [
        MarketLine("result3", "home", 2.0, n_books=4, book_prices=[2.0] * 4),
        MarketLine("result3", "draw", 3.4, n_books=4, book_prices=[3.4] * 4),
        MarketLine("result3", "away", 3.8, n_books=4, book_prices=[3.8] * 4),
        MarketLine("dc", "home_draw", 1.3, n_books=1, book_prices=[1.3]),
        MarketLine("btts", "yes", 1.8, n_books=2, book_prices=[1.8, 1.8]),
        MarketLine("btts", "no", 1.95, n_books=2, book_prices=[1.95, 1.95]),
        MarketLine("total", "over", 1.9, line=2.5, n_books=3, book_prices=[1.9] * 3),
        MarketLine("total", "under", 1.95, line=2.5, n_books=3, book_prices=[1.95] * 3),
    ]
    e.home_id, e.away_id, e.canonical_competition_id = home_id, away_id, COMP
    return e
