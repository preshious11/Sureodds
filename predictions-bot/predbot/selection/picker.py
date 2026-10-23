"""Collection orchestration over persisted canonical predictions."""
from dataclasses import dataclass, field
from predbot.selection.top import select_top
from predbot.selection.daily import select_daily


@dataclass
class PickSet:
    top: list = field(default_factory=list)
    daily: list = field(default_factory=list)
    pool_size: int = 0
    eligible: int = 0
    diagnostics: dict = field(default_factory=dict)
    complete: bool = True
    predictions: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    ticket_predictions: list = field(default_factory=list)
    candidate_timezone: str = ""

    def all(self):
        return self.predictions


def prediction_identity(row):
    """WHY: compare prediction semantics, never display labels or provider spellings."""
    from predbot.selection.representatives import FAMILY_OF_MARKET
    from predbot.providers.base import SIDE_MARKETS
    market = str(row.get('market') or '').lower()
    market = {'1x2': 'result3', 'moneyline': 'result2', 'over_under': 'total',
              'spread': 'handicap', 'double_chance': 'dc', 'draw_no_bet': 'dnb'}.get(market, market)
    line = row.get('line')
    entity = row.get('entity') or 'all'
    if market in SIDE_MARKETS and entity in ('home', 'away'):
        entity = 'all'  # same canonicalization as MarketLine; the side is selection
    return (row.get('canonical_event_id') or row.get('fixture_key'),
            FAMILY_OF_MARKET.get(market, market), row.get('stat'), entity, row.get('selection'),
            None if line is None else round(float(line), 2), row.get('period'))


class PredictionPool:
    """Today's prediction pool behind one interface, whatever holds it.

    ``persistent`` is the production pool (the shared SQLite store); ``scratch`` is a throwaway in-memory store
    for dry runs. Both are driven by the same three calls - ``store`` (canonical rows, first-write-wins),
    ``package`` (the Top and Daily selectors over the STORED rows, recording memberships) and ``rows`` - so the
    selectors and the row format exist once and a dry run cannot drift from production. ``close`` discards a
    scratch pool; it never touches a persistent one."""

    def __init__(self, db, day, scratch=False):
        self.db, self.day, self.scratch = db, day, scratch

    @classmethod
    def persistent(cls, db, day):
        return cls(db, day)

    @classmethod
    def scratch(cls, day):
        from predbot.storage.db import Database
        return cls(Database(':memory:'), day, scratch=True)

    def transaction(self):
        return self.db.transaction()

    def store(self, pickset):
        from predbot.jobs.daily_picks import store_picks
        store_picks(pickset, self.db, self.day)

    def rows(self, collection='all'):
        return [dict(r) for r in self.db.collection_predictions(collection, self.day)]

    def package(self, cfg, now_utc):
        """Selectors see immutable stored rows only. Memberships never copy predictions.

        Rejection logging (Part 4, 2026-10-23): a durable ``reject_sink`` is wired in for a PERSISTENT
        pool only - a scratch pool's database is discarded the moment ``close()`` runs (see the class
        docstring), so logging into it would just be wasted writes. Either way, ``select_top``/
        ``select_daily``'s return values here are identical to passing no sink at all - see
        tests/test_rejection_logging.py."""
        db, day = self.db, self.day
        has_archive = bool(db.query('SELECT 1 FROM ticket_candidates WHERE pick_date=? LIMIT 1', (day,)))
        pool = db.ticket_candidate_rows(day) if has_archive else self.rows('all')
        top_sink = top_flush = daily_sink = daily_flush = None
        if not self.scratch:
            from predbot.selection.rejections import collector, new_run_id
            run_id, sample = new_run_id(), cfg.rejection_sample_per_reason
            top_sink, top_flush = collector(db, day, 'top', run_id, sample)
            daily_sink, daily_flush = collector(db, day, 'daily', run_id, sample)
        top, top_diag = select_top(pool, cfg, now_utc, reject_sink=top_sink)
        if top_flush and (errors := top_flush(top)):
            top_diag['rejection_log_errors'] = errors
        # WHY: Top owns identical predictions; another market on the same fixture remains eligible.
        top_keys = {prediction_identity(row) for row in top}
        daily_pool = [row for row in pool if prediction_identity(row) not in top_keys]
        daily, daily_diag = select_daily(daily_pool, cfg, now_utc, reject_sink=daily_sink)
        if daily_flush and (errors := daily_flush(daily)):
            daily_diag['rejection_log_errors'] = errors
        # WHY: Worker top/daily receipts freeze both cards, just like legacy pick receipts.
        with db.transaction():
            if not db.deliveries_exist('picks', day) and not db.query(
                    "SELECT 1 FROM posts WHERE post_date=? AND kind IN ('picks','picks_top','picks_daily','top','daily')", (day,)):
                db.execute("DELETE FROM prediction_memberships WHERE collection IN ('top','daily') "
                           "AND prediction_id IN (SELECT id FROM picks WHERE pick_date=?)", (day,))
                for collection, rows in (('top', top), ('daily', daily)):
                    for rank, row in enumerate(rows, 1):
                        pid = db.store_ticket_prediction(row) if isinstance(row['id'], str) else row['id']
                        db.add_membership(pid, collection, rank)
        return {'top': top_diag, 'daily': daily_diag,
                'all_predictions': len(self.rows('all')), 'ticket_candidates': len(pool), 'top_selected': len(self.rows('top')),
                'daily_selected': len(self.rows('daily'))}

    def close(self):
        if self.scratch:
            self.db.close()


def package_predictions(db, day, cfg, now_utc):
    return PredictionPool.persistent(db, day).package(cfg, now_utc)
