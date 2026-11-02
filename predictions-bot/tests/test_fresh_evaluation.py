import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

from predbot.config import Config
from predbot.jobs import daily_picks
from tests.real_events import real_ev
from tests.conftest import dt
from tests.test_picker import _fx, sc
from tests.real_events import pickset

spec = importlib.util.spec_from_file_location('fresh_evaluation', Path(__file__).resolve().parents[1] / 'tools/fresh_evaluation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_actions_annotations_are_lossless_and_bounded():
    import json
    summary = {'data': '%\n' * 5000}
    lines = list(module.notice_lines(summary))
    assert all(len(line.encode('utf-8')) < 4096 for line in lines)
    parts = [line.split('::', 2)[2].replace('%0A', '\n').replace('%0D', '\r').replace('%25', '%') for line in lines]
    assert json.loads(''.join(parts)) == summary


def test_fresh_evaluation_selects_strict_tickets_without_writing_source(db):
    predictions = [sc(_fx(i), 'result2', 'home', .90,
                      1.44 if i < 2 else 2.20) for i in range(12)]
    for s in predictions:
        s.event.sport = 'basketball'
        s.line.stat = 'points'
    ps = pickset(predictions)
    ps.ticket_predictions = predictions
    ps.diagnostics = {'fixtures_discovered': 12}
    before = list(db.query('SELECT * FROM picks'))
    report = module.evaluate(db, Config(), dt(), None, lambda *args: ps)
    assert report['generation']['fixtures_discovered'] == 12
    for kind, low, high in [('top', 2, 2.5), ('daily', 10, 12)]:
        assert report['tickets'][kind]['selections']
        assert low <= report['tickets'][kind]['combined_odds'] <= high
    assert list(db.query('SELECT * FROM picks')) == before
    assert report['production_applied'] is False
    assert report['telegram_sent'] is False


def test_fresh_evaluation_never_substitutes_started_or_below_target_picks(db):
    predictions = [sc(_fx(1), 'result3', 'home', .9, 2.1),
                   sc(_fx(2), 'result3', 'home', .9, 1.3)]
    predictions[0].event.start = dt(hh=10)
    ps = pickset(predictions)
    ps.ticket_predictions = predictions
    report = module.evaluate(db, Config(), dt(), None, lambda *args: ps)
    assert report['tickets']['top'] == {'selections': [], 'combined_odds': None}
    assert report['tickets']['daily'] == {'selections': [], 'combined_odds': None}
    assert not db.query('SELECT * FROM picks')


def test_required_partial_provider_stays_incomplete_through_fresh_report(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime(2030, 6, 1, 10, tzinfo=timezone.utc)
    events = [real_ev(i, start=now + timedelta(hours=4)) for i in range(3)]
    for i, event in enumerate(events):
        event.canonical_event_id = f'fixture-{i}'
    chain = Mock()
    chain.collect.return_value = events
    chain.last_report = {
        'sharpapi': {'status': 'partial'},
        'oddsapi': {'status': 'complete'},
    }
    chain.order = ['sharpapi', 'oddsapi']
    chain.providers = {}
    monkeypatch.setattr(daily_picks, 'resolve_events_detailed', lambda batch, *args: (batch, []))
    monkeypatch.setattr(daily_picks, 'score_events', lambda batch, *args: [
        sc(event, 'result3', 'home', .76, 1.5) for event in batch])
    cfg = Config(timezone='UTC', provider_fallback_enabled=False)
    generated = []

    def build(*args):
        pickset = daily_picks.build_picks(*args)
        generated.append(pickset)
        return pickset

    report = module.evaluate(db, cfg, now, chain, builder=build)
    assert generated[0].complete is False
    assert generated[0].diagnostics['complete'] is False
    assert generated[0].diagnostics['provider_coverage']['missing_required'] == ['sharpapi']
    assert report['complete'] is False
    assert report['generation']['complete'] is False
    serialized = json.loads(json.dumps(report))
    assert serialized['complete'] is False
    assert serialized['generation']['providers'] == chain.last_report
