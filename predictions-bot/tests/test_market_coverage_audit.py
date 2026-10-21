"""Offline coverage accounting must never widen eligibility or touch sources."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from predbot.config import Config
from predbot.markets.settlement import ResultPath
from predbot.model.engine import representative_predictions
from predbot.model.coverage import scope_counts
from predbot.selection.picker import PickSet, PredictionPool
from predbot.storage.db import Database
from predbot.util.timeutil import iso
from tests.conftest import dt
from tests.test_custom_pick_candidates import alternatives
from tools.audit_market_coverage import audit_database, fixture_inventory, offline, safe_config, write_matrix


def test_diagnostics_distinguish_display_reduction_from_candidate_loss():
    values = alternatives()
    before = deepcopy([(s.p_model, s.line.key(), s.price) for s in values])
    result = representative_predictions(values)
    coverage = result.diagnostics['market_coverage']
    assert sum(r['outcomes'] for r in coverage['scored']) == len(values)
    assert sum(r['outcomes'] for r in coverage['validated']) == len(values)
    assert sum(r['outcomes'] for r in coverage['representatives']) == len(result.predictions) < len(values)
    assert result.ticket_predictions == values
    assert [(s.p_model, s.line.key(), s.price) for s in values] == before
    totals = next(r for r in coverage['validated'] if r['market'] == 'total')
    assert totals['lines'] == [1.5, 2.5, 3.5]
    assert totals['selections'] == ['over', 'under']


def test_period_stat_entity_and_rejections_are_not_collapsed_into_a_family():
    values = alternatives()[:1]
    extra = deepcopy(values[0])
    extra.line = replace(extra.line, market='total', selection='over', period='1h',
                         stat='corners', entity='home', line=2.5)
    values.append(extra)
    result = representative_predictions(values, lambda _: [ResultPath('test', frozenset({'full'}), frozenset({'points'}))])
    coverage = result.diagnostics['market_coverage']
    assert len(coverage['scored']) == 2
    assert len(coverage['validated']) == 1
    assert result.diagnostics['line_rejections'] == {'missing_required_stats': 1}
    rejected_scope = next(r for r in coverage['scored'] if r['market'] == 'total')
    assert (rejected_scope['period'], rejected_scope['stat'], rejected_scope['entity']) == ('1h', 'corners', 'home')


def test_reporting_does_not_change_persistence_or_promoted_ticket():
    values = alternatives()
    result = representative_predictions(values)
    cfg = Config()
    pool = PredictionPool(Database(':memory:'), '2030-06-01')
    try:
        pool.store(PickSet(predictions=result.predictions, ticket_predictions=result.ticket_predictions))
        rows_before = pool.db.ticket_candidate_rows(pool.day)
        scope_counts(values)
        assert pool.db.ticket_candidate_rows(pool.day) == rows_before
        assert len(rows_before) == len(values)
        before = pool.package(cfg, iso(dt(hh=12)))
        def ticket_contracts():
            return [[tuple(r[k] for k in ('canonical_event_id', 'market', 'period', 'stat',
                                          'entity', 'line', 'selection', 'prob', 'price'))
                     for r in pool.rows(kind)] for kind in ('top', 'daily')]
        tickets = ticket_contracts()
        pool.db.mark_post('picks_top', pool.day, 'offline-test-receipt')
        scope_counts(values)
        assert pool.package(cfg, iso(dt(hh=12))) == before
        assert ticket_contracts() == tickets
    finally:
        pool.db.close()


def test_audit_does_not_initialize_or_modify_a_legacy_database(tmp_path):
    path = tmp_path / 'legacy.db'
    source = sqlite3.connect(path)
    source.execute('CREATE TABLE old_marker(value TEXT)')
    source.execute("INSERT INTO old_marker VALUES('preserve')")
    source.commit()
    source.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with offline():
        result = audit_database(path, Config())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert result['archives']['ticket_candidates'] == {'available': False}
    source = sqlite3.connect(path)
    assert source.execute('SELECT value FROM old_marker').fetchone()[0] == 'preserve'
    assert source.execute('SELECT count(*) FROM sqlite_master WHERE type="table"').fetchone()[0] == 1
    source.close()


def test_offline_audit_blocks_accidental_http_requests():
    import requests
    with offline(), pytest.raises(RuntimeError, match='forbids network'):
        requests.get('https://example.invalid')


def test_saved_provider_rows_are_inventory_not_assumed_entitlements():
    with offline():
        inventory = fixture_inventory(Path(__file__).parent / 'fixtures', Config())
    wide = next(r for r in inventory if r['fixture'] == 'sharp_pl_wide_main.json')
    assert wide['raw_rows'] == wide['parsed_rows'] == 200
    assert any(r['market'] == 'total' and r['entity'] == 'home' for r in wide['scopes'])
    mlb = next(r for r in inventory if r['fixture'] == 'sharp_mlb_main.json')
    assert mlb['unparsed']['player_total_bases'] == 90
    assert mlb['unparsed']['total_runs_odd_even'] == 10
    assert mlb['unparsed']['1st_inning_total_runs'] == 10
    assert mlb['unparsed']['total_home_runs'] == 2
    assert mlb['semantic_review'] == {}
    assert mlb['parsed_rows'] + sum(mlb['unparsed'].values()) == mlb['raw_rows']


def test_matrix_keeps_zero_observations_and_configuration_omits_credentials(tmp_path):
    import csv
    config = safe_config(Config(sharpapi_key='do-not-export', sgo_key='do-not-export'))
    assert 'do-not-export' not in json.dumps(config)
    assert 'mma' not in config['allowed_sports']
    report = {'configuration': config, 'databases': [{'source': 'offline', 'replays': [{
        'day': '2030-06-01', 'events_in_saved_cache': 1, 'normalized': [],
        'diagnostics': {'market_coverage': {'scored': [], 'validated': [], 'representatives': []}}}]}]}
    path = tmp_path / 'matrix.csv'
    write_matrix(report, path)
    with path.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    from predbot.markets.catalog import CATALOG
    assert len(rows) == len(config['allowed_sports']) * len(CATALOG)
    assert all(row['validated'] == '0' for row in rows)


def test_invalid_probabilities_stay_rejected_with_reporting():
    values = alternatives()[:4]
    for value, p in zip(values, [float('nan'), 0., 1., float('inf')]):
        value.p_model = p
    result = representative_predictions(values)
    assert result.candidates == result.ticket_predictions == []
    assert result.diagnostics['line_rejections'] == {'invalid_probability': 4}
    assert result.diagnostics['market_coverage']['validated'] == []
