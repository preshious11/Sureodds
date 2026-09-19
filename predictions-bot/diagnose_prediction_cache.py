"""Read-only SQLite prediction cache diagnostic.

Reports today's ``prediction_cache`` row for the configured ``DB_PATH`` without
generation, migration, Telegram sends or sports API calls. Only the standard
library ``sqlite3`` module and the config/time helpers are imported; no provider
or HTTP module is loaded.

Usage: ``python diagnose_prediction_cache.py``
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from predbot.config import Config, load_config
from predbot.util import timeutil

BACKEND = "sqlite"
PREDICTION_TABLE = "prediction_cache"
PROVIDER_TABLE = "provider_cache"


def _count_sport(picks: List[Dict[str, Any]], sport: str) -> int:
    return sum(1 for p in picks if str(p.get("sport", "")).lower() == sport)


def report(cfg: Config) -> Dict[str, Any]:
    """Collect cache status as a dict. Never writes, never calls providers."""
    path = Path(cfg.db_path).resolve()
    out: Dict[str, Any] = {"backend": BACKEND, "db_path": str(path), "db_exists": path.is_file(),
                           "prediction_table": PREDICTION_TABLE, "provider_table": PROVIDER_TABLE,
                           "timezone": cfg.timezone, "today": None}
    if not path.is_file():
        return out
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {r["name"] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
            (PREDICTION_TABLE, PROVIDER_TABLE))}
        out["prediction_table_exists"] = PREDICTION_TABLE in tables
        out["provider_table_exists"] = PROVIDER_TABLE in tables
        if PROVIDER_TABLE in tables:
            out["provider_cache_rows"] = connection.execute(
                f"SELECT COUNT(*) AS n FROM {PROVIDER_TABLE}").fetchone()["n"]
        if PREDICTION_TABLE not in tables:
            return out
        day = timeutil.to_local(timeutil.utcnow(), cfg.timezone).date().isoformat()
        out["local_date"] = day
        out["cache_key"] = f"daily_predictions:{cfg.timezone}:{day}"
        row = connection.execute(f"SELECT * FROM {PREDICTION_TABLE} WHERE cache_key=?",
                                 (out["cache_key"],)).fetchone()
        if row is None:
            out["today"] = "MISS"
            return out
        payload = json.loads(row["payload_json"])
        top = payload.get("top_picks", [])
        daily = payload.get("daily_picks", [])
        all_picks = top + daily
        out["today"] = "HIT"
        out.update({
            "status": row["status"],
            "sport_status": json.loads(row["sport_status_json"]),
            "football_count": _count_sport(all_picks, "football"),
            "baseball_count": _count_sport(all_picks, "baseball"),
            "top_picks_count": len(top),
            "daily_picks_count": len(daily),
            "generated_at": row["generated_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "retry_after": row["retry_after"] if row["status"] == "PARTIAL" else None,
            "cache_version": row["cache_version"],
        })
    finally:
        connection.close()
    return out


def _display_ts(raw: Optional[str], tz: str) -> str:
    """Human-readable local timestamp ('2026-09-17 12:35 PM WAT') for a stored
    ISO-8601 UTC value ('2026-09-17T11:35:24Z'). The trailing ``Z`` marker is
    correct ISO-8601 for UTC but reads like a placeholder; diagnostics show the
    named local zone instead. Storage keeps the machine-readable ISO format."""
    dt = timeutil.parse_iso(raw or "")
    if dt is None:
        return str(raw or "n/a")
    local = timeutil.to_local(dt, tz)
    return f"{local:%Y-%m-%d} {local.strftime('%I:%M %p').lstrip('0')} {local.tzname() or tz}"


def render(info: Dict[str, Any]) -> List[str]:
    lines = [f"Prediction cache backend={info['backend']}",
             f"Database path: {info['db_path']} | exists={info['db_exists']}",
             f"Prediction cache table: {info['prediction_table']}",
             f"Provider cache table: {info['provider_table']}"]
    if not info["db_exists"]:
        lines.append("No database exists here; check DB_PATH and the persistent mount.")
        return lines
    if not info.get("prediction_table_exists"):
        lines.append(f"{PREDICTION_TABLE} table: absent; startup creates it automatically.")
        return lines
    lines.append(f"Provider cache rows: {info.get('provider_cache_rows', 0)}")
    lines.append(f"Today ({info['local_date']} {info['timezone']}): cache {info['today']}")
    if info["today"] != "HIT":
        return lines
    lines += [f"Status: {info['status']}",
              f"Sport status: {json.dumps(info['sport_status'], sort_keys=True)}",
              f"Football count: {info['football_count']}",
              f"Baseball count: {info['baseball_count']}",
              f"Top Picks count: {info['top_picks_count']}",
              f"Daily Picks count: {info['daily_picks_count']}",
              f"Generated time: {_display_ts(info['generated_at'], info['timezone'])}",
              f"Updated time: {_display_ts(info['updated_at'], info['timezone'])}",
              f"Expires time: {_display_ts(info['expires_at'], info['timezone'])}"]
    if info["status"] == "PARTIAL":
        lines.append(f"Retry-after: {_display_ts(info['retry_after'], info['timezone'])}")
    lines.append(f"Cache version: {info['cache_version']}")
    return lines


def main(cfg: Optional[Config] = None) -> Dict[str, Any]:
    info = report(cfg or load_config())
    print("\n".join(render(info)))
    return info


if __name__ == "__main__":
    main()
