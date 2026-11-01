import importlib.util
from pathlib import Path

from predbot.config import Config
from tests.conftest import dt
from tests.test_picker import _fx, sc
from tests.real_events import pickset

spec = importlib.util.spec_from_file_location('fresh_evaluation', Path(__file__).resolve().parents[1] / 'tools/fresh_evaluation.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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
