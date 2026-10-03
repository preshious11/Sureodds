"""Diagnostic only: one offline replay generation -> the pipeline funnel and the All Predictions mix (fixtures x families).

    python tools/replay_funnel.py [--json out.json]

Uses the recorded provider cache in tests/fixtures/replay (no network, temporary database copy). It changes nothing
and tunes nothing: it prints what the current rules produce."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import replay  # noqa: E402


def funnel(capture_scored: list | None = None) -> dict:
    from predbot.cache.fetcher import UnifiedFetcher
    from predbot.cache.providers import collection_signature
    from predbot.config import Config
    from predbot.model import engine
    from predbot.providers.chain import ProviderChain, build_providers
    from predbot.storage.db import Database
    from predbot.util.pipeline_report import pipeline_lines

    tmp = tempfile.mkdtemp(prefix="predbot-funnel-")
    path = os.path.join(tmp, "replay.db")
    shutil.copyfile(replay.REPLAY_DB, path)
    db = Database(path)
    try:
        with replay.replay_env(db.get_kv("replay:now")):
            cfg = Config()
            for row in db.query("SELECT cache_key, metadata_json FROM provider_cache"):
                meta = json.loads(row["metadata_json"])
                meta["collection_signature"] = collection_signature(row["cache_key"].rsplit(":", 1)[-1], cfg)
                db.execute("UPDATE provider_cache SET metadata_json=? WHERE cache_key=?",
                           (json.dumps(meta, sort_keys=True), row["cache_key"]))
            chain = ProviderChain(build_providers(cfg, db), cfg.provider_order)
            original = engine.representative_predictions

            def spy(scored, *a, **k):
                if capture_scored is not None:
                    capture_scored.extend(scored)
                return original(scored, *a, **k)

            engine.representative_predictions = spy
            try:
                snapshot = UnifiedFetcher(chain, db, cfg).get_daily_snapshot(persist=True, scheduled=True)
            finally:
                engine.representative_predictions = original
            gen = snapshot.generation
            fixtures = {p.fixture_id for p in snapshot.all_predictions}
            by_sport = Counter(p.sport for p in snapshot.all_predictions)
            fixtures_by_sport = {sp: len({p.fixture_id for p in snapshot.all_predictions if p.sport == sp}) for sp in by_sport}

            def dup_fixtures(picks):
                return sum(1 for n in Counter(p.fixture_id for p in picks).values() if n > 1)

            return {"date": snapshot.date, "lines": pipeline_lines(gen), "diagnostics": replay.strip(gen),
                    "fixtures": len(fixtures), "all_predictions": len(snapshot.all_predictions),
                    "predictions_per_fixture": round(len(snapshot.all_predictions) / max(1, len(fixtures)), 3),
                    "by_sport": dict(by_sport), "fixtures_by_sport": fixtures_by_sport,
                    "per_fixture_by_sport": {sp: round(by_sport[sp] / fixtures_by_sport[sp], 2) for sp in by_sport},
                    "by_family": dict(Counter(p.market for p in snapshot.all_predictions)),
                    "top_markets": dict(Counter(p.market for p in snapshot.top_picks)),
                    "daily_markets": dict(Counter(p.market for p in snapshot.daily_picks)),
                    "top_duplicate_fixtures": dup_fixtures(snapshot.top_picks),
                    "daily_duplicate_fixtures": dup_fixtures(snapshot.daily_picks)}
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    out = funnel()
    print("\n".join(out["lines"]))
    print(f"\nFixtures: {out['fixtures']} | All Predictions: {out['all_predictions']} | per fixture: {out['predictions_per_fixture']}")
    print("By sport:", out["by_sport"], "| fixtures:", out["fixtures_by_sport"], "| per fixture:", out["per_fixture_by_sport"])
    print("By family (market key):", out["by_family"])
    print("Top markets:", out["top_markets"], "| duplicate fixtures:", out["top_duplicate_fixtures"])
    print("Daily markets:", out["daily_markets"], "| duplicate fixtures:", out["daily_duplicate_fixtures"])
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(out, indent=1, default=str), encoding="utf8")
