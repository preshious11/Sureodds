"""Events on supported competitions with one quoted market, so they survive canonical identity resolution
(unknown leagues are rejected) and the engine's missing-odds check."""
from predbot.providers.base import MarketLine
from tests.test_picker import ev as _ev

LEAGUES = {"football": "England - Premier League", "baseball": "MLB", "basketball": "NBA",
           "american_football": "NFL", "tennis": "ATP"}


def real_ev(i, home=None, away=None, sport="football", start=None, league=None):
    e = _ev(i, home=home, away=away, sport=sport, start=start, league=league or LEAGUES[sport])
    e.markets = [MarketLine("result3" if sport == "football" else "result2", "home", 1.5, n_books=5)]
    return e


def pickset(scored, cfg=None):
    """A generation result for hand-built scored predictions: canonical ids and evidence set as production does."""
    from predbot.config import Config
    from predbot.selection.evidence import assess
    from predbot.selection.picker import PickSet
    from predbot.util.fixtures import fixture_key
    cfg, tables = cfg or Config(), {}
    for s in scored:
        s.event.canonical_event_id = s.event.canonical_event_id or fixture_key(s.event)
        e = assess(s, cfg, tables)
        s.quality, s.se, s.p_lcb, s.rank_score = e.quality, e.se, e.p_lcb, e.rank
        s.notes['evidence'] = e.as_notes()
        s.notes['independent'] = s.independent
    ps = PickSet(predictions=list(scored), pool_size=len(scored), eligible=len(scored))
    ps.complete = True
    return ps
