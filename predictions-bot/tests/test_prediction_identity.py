"""Prediction identity and immutable posted-ticket memberships."""
from dataclasses import replace
from predbot.config import Config
from predbot.selection.picker import prediction_identity, PredictionPool
from predbot.storage.db import Database
from tests.conftest import dt
from tests.test_daily_own_sizing import pool
from tests.pipeline_helpers import select, store_selected


def test_identity_normalizes_family_labels_and_lines():
    row = dict(canonical_event_id='e', fixture_key='f', market='result3', stat='points',
               selection='home', line=1.234, period='full', label='Home wins')
    renamed = dict(row, market='1X2', label='Completely different display', line=1.231)
    assert prediction_identity(row) == prediction_identity(renamed)
    assert prediction_identity(row) != prediction_identity(dict(row, market='btts'))
    assert prediction_identity(dict(row, line=None))[-2] is None
    assert prediction_identity(dict(row, canonical_event_id=''))[0] == 'f'


def test_identical_top_prediction_is_excluded_from_daily_and_caps_hold():
    cfg = Config()
    ps = select(pool(), cfg, dt(), raw=True)
    keys = lambda rows: {prediction_identity(dict(canonical_event_id=s.event.canonical_event_id,
        market=s.line.market, stat=s.line.stat, selection=s.line.selection, line=s.line.line,
        period=s.line.period)) for s in rows}
    assert ps.top and ps.daily
    assert not keys(ps.top) & keys(ps.daily)
    assert len(ps.daily) <= cfg.daily_target
    assert len({s.event.canonical_event_id for s in ps.daily}) == len(ps.daily)


def test_same_fixture_different_market_is_still_allowed(monkeypatch):
    import predbot.selection.picker as picker
    from unittest.mock import Mock
    from contextlib import nullcontext
    top = dict(id=1, canonical_event_id='e', market='result3', stat='points', selection='home', line=None, period='full')
    duplicate = dict(top, id=2, market='1X2', label='Another spelling')
    alternate = dict(top, id=3, market='btts', selection='yes')
    db = Mock()
    db.query.return_value = []
    db.deliveries_exist.return_value = False
    db.transaction.side_effect = nullcontext
    storage = PredictionPool(db, '2030-06-01')
    storage.rows = lambda collection='all': [top, duplicate, alternate] if collection == 'all' else []
    monkeypatch.setattr(picker, 'select_top', lambda rows, *args, **kwargs: ([top], {}))
    seen = []
    def daily(rows, *args, **kwargs):
        seen.extend(rows)
        return rows, {}
    monkeypatch.setattr(picker, 'select_daily', daily)
    storage.package(Config(), dt().isoformat())
    assert seen == [alternate]


def test_worker_post_receipts_freeze_both_memberships():
    cfg = Config()
    scored = pool()
    ps = select(scored, cfg, dt(), raw=True)
    for kind in ('top', 'daily'):
        db = Database(':memory:')
        try:
            day = dt().date().isoformat()
            store_selected(ps, db, day)
            before = [tuple(r) for r in db.query('SELECT * FROM prediction_memberships ORDER BY prediction_id,collection')]
            db.mark_post(kind, day, "123")
            # Changed selection settings must never replace either already-issued ticket.
            PredictionPool.persistent(db, day).package(replace(cfg, top_legs_max=1, daily_target=1), dt().isoformat())
            after = [tuple(r) for r in db.query('SELECT * FROM prediction_memberships ORDER BY prediction_id,collection')]
            assert after == before
        finally:
            db.close()
