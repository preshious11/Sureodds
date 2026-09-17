"""Provider sport-capability discovery, cached in SQLite.

Capabilities come from each provider's own catalog endpoint - never from a
hand-written sport list.  The cache (key ``capabilities:v1:<provider>`` in the
``kv`` table, TTL default 24h) exists so sport catalogs are not re-requested on
every daily run, PARTIAL retry or button press: discovery costs one metadata
call per provider per TTL window.

Payload shape (what each provider can actually reach with the current key):
    {"provider": ..., "fetched_at": iso, "expires_at": iso,
     "sports": [{"key": provider sport id, "sport": canonical sport or the raw
                 key when unmapped, "supported": bool, "events": int,
                 "live_events": int, "leagues": [...]}],
     "leagues": {"<league id>": {"sport": canonical sport, ...}},
     "unsupported": {"<provider sport id>": events}}   # DISCOVERED_UNSUPPORTED_SPORT
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from predbot.util.timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

CACHE_PREFIX = "capabilities:v1:"
DEFAULT_TTL = timedelta(hours=24)


def _key(provider: str) -> str:
    return f"{CACHE_PREFIX}{provider}"


def load_cached(db, provider: str, ttl: timedelta = DEFAULT_TTL) -> Optional[Dict]:
    """The cached catalog for ``provider`` when present and fresh, else None."""
    if db is None:
        return None
    try:
        raw = db.get_kv(_key(provider))
    except Exception as e:  # noqa: BLE001
        log.debug("capability cache read failed (%s): %s", provider, e)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        expires = parse_iso(payload.get("expires_at", ""))
        if expires and utcnow() < expires:
            return payload
    except (ValueError, TypeError, KeyError):
        log.warning("capability cache invalid for %s; refetching", provider)
    return None


def store(db, provider: str, payload: Dict, ttl: timedelta = DEFAULT_TTL) -> Dict:
    """Persist a catalog payload with an absolute expiry; returns the payload."""
    if db is None:
        return payload
    now = utcnow()
    payload = dict(payload)
    payload["fetched_at"] = payload.get("fetched_at") or iso(now)
    payload["expires_at"] = iso(now + ttl)
    try:
        db.set_kv(_key(provider), json.dumps(payload))
    except Exception as e:  # noqa: BLE001
        log.debug("capability cache write failed (%s): %s", provider, e)
    return payload


def supported_sports(payload: Optional[Dict]) -> List[str]:
    """Canonical sports a provider can reach, biggest catalog first."""
    if not payload:
        return []
    rows = [s for s in payload.get("sports", []) if s.get("supported") and s.get("events")]
    rows.sort(key=lambda s: -int(s.get("events") or 0))
    return [s["sport"] for s in rows]


def unavailable_sports(payload: Optional[Dict]) -> Dict[str, int]:
    """DISCOVERED_UNSUPPORTED_SPORT: reachable but no canonical mapping yet."""
    if not payload:
        return {}
    return dict(payload.get("unsupported") or {})
