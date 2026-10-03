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

    def all(self):
        return self.predictions


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
        """Selectors see immutable stored rows only. Memberships never copy predictions."""
        db, day = self.db, self.day
        pool = self.rows('all')
        top, top_diag = select_top(pool, cfg, now_utc)
        daily, daily_diag = select_daily(pool, cfg, now_utc)
        with db.transaction():
            if not db.deliveries_exist('picks', day) and not db.query(
                    "SELECT 1 FROM posts WHERE post_date=? AND kind IN ('picks','picks_top','picks_daily')", (day,)):
                db.execute("DELETE FROM prediction_memberships WHERE collection IN ('top','daily') "
                           "AND prediction_id IN (SELECT id FROM picks WHERE pick_date=?)", (day,))
                for collection, rows in (('top', top), ('daily', daily)):
                    for rank, row in enumerate(rows, 1):
                        db.add_membership(row['id'], collection, rank)
        return {'top': top_diag, 'daily': daily_diag,
                'all_predictions': len(pool), 'top_selected': len(self.rows('top')),
                'daily_selected': len(self.rows('daily'))}

    def close(self):
        if self.scratch:
            self.db.close()


def package_predictions(db, day, cfg, now_utc):
    return PredictionPool.persistent(db, day).package(cfg, now_utc)
