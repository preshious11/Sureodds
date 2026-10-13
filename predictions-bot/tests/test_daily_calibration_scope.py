"""Daily scoring must not apply legacy calibration to settlement-aware quotes."""
from types import SimpleNamespace

import pytest

from predbot.jobs import daily_picks
from predbot.model.calibration import Calibrator
from predbot.storage.db import Database


def test_daily_scoring_uses_corrected_scope_and_preserves_legacy_calibration(monkeypatch):
    db = Database(":memory:")
    try:
        db.set_calibration("global", .5, -.6, 200)
        db.set_calibration("settlement_v2", 1.5, .6, 200)
        before = [tuple(row) for row in db.query("SELECT * FROM calibration")]
        seen = []

        class Model:
            def __init__(self, event, elo, calibrator, **kwargs):
                seen.append(calibrator)

            def score_all(self):
                return [seen[-1].apply(.7)]

        monkeypatch.setattr(daily_picks, "EventModel", Model)
        cfg = SimpleNamespace(independent_model_mode="off", model_shrink=.8)
        result = daily_picks.score_events([object()], db, cfg)
        assert len(seen) == 1 and seen[0].scope == "settlement_v2"
        assert result == pytest.approx([Calibrator(db, scope="settlement_v2").apply(.7)])
        assert result != pytest.approx([Calibrator(db, scope="global").apply(.7)])
        assert [tuple(row) for row in db.query("SELECT * FROM calibration")] == before
    finally:
        db.close()
