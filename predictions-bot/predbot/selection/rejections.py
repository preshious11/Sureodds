"""Durable rejection logging for select_top()/select_daily().

Pure instrumentation. ``collector()`` returns a ``(sink, flush)`` pair: ``sink`` is passed as
``reject_sink`` into ``select_top``/``select_daily`` (predbot/selection/top.py, daily.py), which call
it at the exact point they already increment their in-memory ``rejected`` Counter. Nothing about what
gets selected or returned ever depends on whether a sink is given (tests/test_rejection_logging.py).

Volume control: below_probability, disabled_market and insufficient_books get one summary row per
(pick_date, product, reason) with the day's total count, plus a deterministic sample of at most
``sample_size`` full-detail rows. The sample is chosen by sorting candidates on a hash of
(pick_date, product, reason, candidate identity), so the same candidate set always yields the same
sample, and a sampled row is gradeable like any other. Every other reason gets a full-detail row per
rejected candidate.

Rows are buffered until ``flush(selected)``, which receives the FINAL published rows. Search may
publish a combination that includes a row the greedy pass rejected; such rows are never logged as
rejected. Writes are first-write-wins (see migration 0015), so a later run never duplicates a row.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections import Counter, defaultdict
from typing import Callable, Tuple

log = logging.getLogger(__name__)

#: Reasons volume-controlled into a summary count plus a deterministic sample.
SUMMARY_ONLY_REASONS = frozenset({"below_probability", "disabled_market", "insufficient_books"})


def new_run_id() -> str:
    return f"{os.environ.get('GITHUB_RUN_ID') or 'local'}-{uuid.uuid4().hex[:8]}"


def _sample_key(db, pick_date: str, product: str, reason: str, row) -> str:
    try:
        identity = db._candidate_identity(row)
    except (KeyError, TypeError):
        identity = json.dumps(row, sort_keys=True, default=str)
    return hashlib.sha256(f"{pick_date}|{product}|{reason}|{identity}".encode()).hexdigest()


def collector(db, pick_date: str, product: str, run_id: str, sample_size: int) -> Tuple[Callable, Callable]:
    """Build a ``reject_sink`` and its matching ``flush`` for one select_top()/select_daily() run.

    ``sink(row, reason, winner_row=None)`` is safe to call for every rejection. ``winner_row`` (only for
    ``duplicate_event``) is the full row of the candidate that took the fixture's slot; its candidate_id is
    computed by ``db.record_rejection`` the same way ``row``'s is. ``flush(selected)`` must be called once
    after selection, with the final published rows, and returns how many writes failed. A failed write is
    logged and counted, never raised: rejection logging is diagnostics and must not abort a generation run."""
    summary: Counter = Counter()
    detail: list = []
    volume_rows = defaultdict(list)

    def sink(row, reason: str, winner_row=None) -> None:
        if reason in SUMMARY_ONLY_REASONS:
            summary[reason] += 1
            volume_rows[reason].append(row)
            return
        detail.append((row, reason, winner_row))

    def flush(selected=()) -> int:
        published = {id(r) for r in selected}
        writes = [(reason, row, winner_row, 1, 0) for row, reason, winner_row in detail if id(row) not in published]
        for reason, rows in volume_rows.items():
            eligible = [r for r in rows if id(r) not in published]
            eligible.sort(key=lambda r: _sample_key(db, pick_date, product, reason, r))
            writes += [(reason, r, None, 1, 1) for r in eligible[:max(0, sample_size)]]
        writes += [(reason, None, None, count, 0) for reason, count in summary.items()]
        errors = 0
        for reason, row, winner_row, count, sampled in writes:
            try:
                db.record_rejection(pick_date, product, reason, row=row, winner_row=winner_row,
                                    count=count, sampled=sampled, run_id=run_id)
            except Exception:                                   # noqa: BLE001 - see docstring
                errors += 1
        if errors:
            log.error("selection_rejections: %d of %d writes failed for %s %s; selection unaffected",
                      errors, len(writes), product, pick_date)
        return errors

    return sink, flush
