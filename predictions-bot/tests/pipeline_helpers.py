"""Test-only harness exercising generation → storage → collection boundaries."""
from predbot.model.engine import representative_predictions
from predbot.selection.evidence import assess
from predbot.selection.picker import PickSet, package_predictions
from predbot.jobs.daily_picks import store_picks
from predbot.storage.db import Database
from predbot.util.fixtures import fixture_key
from predbot.util.timeutil import iso


def select(scored, cfg, now, raw=False, **kwargs):
    """generate -> store -> package. ``raw=True`` skips representative selection and stores the given lines as the
    predictions: the Top / Daily selectors only ever read stored rows, so their mechanics (caps, books, odds windows) are
    exercised on any market, including ones the representative pool does not generate."""
    db = Database(':memory:')
    try:
        for s in scored:
            s.event.canonical_event_id = fixture_key(s.event)
        if raw:
            from predbot.model.engine import Generation
            result = Generation(list(scored), {})
        else:
            result = representative_predictions(scored, kwargs.get('results_gate'))
        tables = {}
        for s in result.predictions:
            e = assess(s, cfg, tables)
            s.quality, s.se, s.p_lcb, s.rank_score = e.quality, e.se, e.p_lcb, e.rank
            s.notes['evidence'] = e.as_notes()
            s.notes['independent'] = s.independent
        ps = PickSet(predictions=result.predictions, diagnostics=result.diagnostics,
                     pool_size=len(scored), eligible=len(result.predictions))
        day = now.date().isoformat()
        store_picks(ps, db, day)
        ps.diagnostics.update(package_predictions(db, day, cfg, iso(now)))
        by_row = {(s.event.canonical_event_id, s.line.market, s.line.selection, s.line.line): s for s in ps.predictions}
        ps.top = [by_row[(r['canonical_event_id'], r['market'], r['selection'], r['line'])] for r in db.collection_predictions('top', day)]
        ps.daily = [by_row[(r['canonical_event_id'], r['market'], r['selection'], r['line'])] for r in db.collection_predictions('daily', day)]
        return ps
    finally:
        db.close()


def _daily_score(s):
    return s.rank_score if s.p_lcb is not None else s.p_model


def store_selected(ps, db, day):
    """Persist a hand-built slate the way production does: each distinct prediction once, then memberships."""
    from predbot.jobs.daily_picks import store_picks
    seen, ordered = {}, []
    for s in list(ps.top) + list(ps.daily) + list(ps.predictions):
        if not s.event.canonical_event_id:
            s.event.canonical_event_id = fixture_key(s.event)
        if id(s) not in seen:
            seen[id(s)] = s
            ordered.append(s)
    full = PickSet(predictions=ordered, diagnostics=ps.diagnostics)
    store_picks(full, db, day)
    ids = {(r['canonical_event_id'], r['market']): r['id'] for r in db.query("SELECT id,canonical_event_id,market FROM picks WHERE pick_date=?", (day,))}
    for collection, chosen in (('top', ps.top), ('daily', ps.daily)):
        for rank, s in enumerate(chosen, 1):
            db.add_membership(ids[(s.event.canonical_event_id, s.line.market)], collection, rank)
