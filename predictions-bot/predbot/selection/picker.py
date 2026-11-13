"""Collection orchestration over persisted canonical predictions."""
from dataclasses import dataclass, field
import math
from predbot.selection.top import select_top
from predbot.selection.daily import select_daily
from predbot.util.timeutil import parse_iso


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


def _canonical_market(market):
    market = str(market or '').lower()
    return {'1x2': 'result3', 'moneyline': 'result2', 'over_under': 'total',
            'spread': 'handicap', 'double_chance': 'dc', 'draw_no_bet': 'dnb'}.get(market, market)


def prediction_identity(row):
    """WHY: compare prediction semantics, never display labels or provider spellings."""
    from predbot.selection.representatives import FAMILY_OF_MARKET
    from predbot.providers.base import SIDE_MARKETS
    market = _canonical_market(row.get('market'))
    line = row.get('line')
    entity = row.get('entity') or 'all'
    if market in SIDE_MARKETS and entity in ('home', 'away'):
        entity = 'all'  # same canonicalization as MarketLine; the side is selection
    return (row.get('canonical_event_id') or row.get('fixture_key'),
            FAMILY_OF_MARKET.get(market, market), row.get('stat'), entity, row.get('selection'),
            None if line is None else round(float(line), 2), row.get('period'))


def _team_side(row):
    """The team a directional pick backs ('home'/'away'), or None for a non-team-sided
    market (totals, btts, props, a draw selection, ...). Used only to catch a Daily leg
    that backs the OPPOSITE team from an already-selected Top leg on the same fixture -
    e.g. Top's "home to win" (result3/result2/dnb) against Daily's "away" handicap. Two
    directional picks on the SAME side (or either side against a non-directional market,
    like a total) are left alone: they are reinforcing or unrelated, not contradictory."""
    from predbot.providers.base import SIDE_MARKETS
    market, selection = _canonical_market(row.get('market')), row.get('selection')
    return selection if market in SIDE_MARKETS and selection in ('home', 'away') else None


class PredictionPool:
    """Today's prediction pool behind one interface, whatever holds it.

    ``persistent`` is the production pool (the shared SQLite store); ``scratch`` is a throwaway in-memory store
    for dry runs. Both are driven by the same three calls - ``store`` (canonical rows, first-write-wins),
    ``package`` (the Top and Daily selectors over the STORED rows, recording memberships) and ``rows`` - so the
    selectors and the row format exist once and a dry run cannot drift from production. ``close`` discards a
    scratch pool; it never touches a persistent one."""

    def __init__(self, db, day, scratch=False):
        self.db, self.day, self.scratch = db, day, scratch
        self.fresh_rows = []
        self.fresh_evaluation = False
        self.evaluation_id = None

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
        from predbot.jobs.daily_picks import prediction_row, store_picks
        store_picks(pickset, self.db, self.day)
        self.fresh_evaluation = bool(pickset.diagnostics.get('generated_at'))
        self.evaluation_id = pickset.diagnostics.get('generated_at')
        for scored in pickset.ticket_predictions:
            row = prediction_row(scored, self.day)
            row['id'] = self.db._candidate_identity(row)
            row['status'] = 'pending'
            self.fresh_rows.append(row)

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
        if self.fresh_evaluation:
            pool = self.fresh_rows
        else:
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
        # Top picks at most one leg per fixture (its own duplicate_event guard), so there is
        # at most one side to record per event here.
        top_sides = {(row.get('canonical_event_id') or row.get('fixture_key')): side
                     for row in top if (side := _team_side(row))}

        def opposes_top(row):
            side = _team_side(row)
            if side is None:
                return False
            eid = row.get('canonical_event_id') or row.get('fixture_key')
            return top_sides.get(eid) not in (None, side)

        daily_pool = []
        for row in pool:
            if prediction_identity(row) in top_keys:
                continue
            if opposes_top(row):
                eid = row.get('canonical_event_id') or row.get('fixture_key')
                winner = next((t for t in top if (t.get('canonical_event_id') or t.get('fixture_key')) == eid), None)
                if daily_sink:
                    daily_sink(row, 'opposes_top_selection', winner_row=winner)
                continue
            daily_pool.append(row)
        daily, daily_diag = select_daily(daily_pool, cfg, now_utc, reject_sink=daily_sink)
        if daily_flush and (errors := daily_flush(daily)):
            daily_diag['rejection_log_errors'] = errors
        active_revision = db.active_ticket_revision(day)
        should_create_revision = bool(top or daily) and (
            self.fresh_evaluation or active_revision is None)
        no_qualifying_ticket = not (top or daily)
        if should_create_revision:
            now = parse_iso(now_utc)

            def revalidate(rows, lower, upper):
                if not rows or now is None:
                    return False
                product = 1.0
                for row in rows:
                    kickoff = parse_iso(row.get('start_utc'))
                    price = row.get('price')
                    if (row.get('status') != 'pending' or kickoff is None or kickoff <= now
                            or not isinstance(price, (int, float)) or isinstance(price, bool)
                            or not math.isfinite(price) or price <= 1):
                        return False
                    product *= price
                return lower <= product <= upper

            if not revalidate(top, cfg.top_odds_target, cfg.top_odds_max):
                top = []
            if not revalidate(daily, cfg.daily_odds_min, cfg.daily_odds_max):
                daily = []
            no_qualifying_ticket = not (top or daily)
            if top or daily:
                evaluation_id = self.evaluation_id or f"package:{now_utc}"
                with db.transaction():
                    revision_id = db.create_ticket_revision(day, evaluation_id, now_utc)
                    for collection, rows in (('top', top), ('daily', daily)):
                        for rank, row in enumerate(rows, 1):
                            if isinstance(row.get('id'), str):
                                prediction_id = db.store_ticket_prediction(
                                    row, revision_id=revision_id, collection=collection)
                            else:
                                prediction_id = int(row['id'])
                                db.add_membership(prediction_id, collection, rank)
        return {'top': top_diag, 'daily': daily_diag,
                'all_predictions': len(self.rows('all')), 'ticket_candidates': len(pool), 'top_selected': len(self.rows('top')),
                'daily_selected': len(self.rows('daily')),
                'active_ticket_revision': db.active_ticket_revision(day),
                'no_qualifying_ticket': no_qualifying_ticket}

    def close(self):
        if self.scratch:
            self.db.close()


def package_predictions(db, day, cfg, now_utc):
    return PredictionPool.persistent(db, day).package(cfg, now_utc)
