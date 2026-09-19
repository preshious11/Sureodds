from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

#: Delivery time used when nothing is configured ("09:00" local, per the brief).
DEFAULT_HHMM = "09:00"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_hhmm(s: str, default: str = DEFAULT_HHMM) -> Tuple[int, int]:
    """"HH:MM" -> (hour, minute); malformed input falls back to ``default``.

    A bad ``POST_TIME``/``RECAP_TIME`` env value must not take the worker down,
    so this never raises: the candidate values are tried in order.
    """
    for candidate in (s, default, DEFAULT_HHMM):
        try:
            h, m = (int(x) for x in str(candidate).split(":"))
        except (ValueError, AttributeError):
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    return 9, 0


def is_valid_tz(name: str) -> bool:
    """True when ``name`` is an IANA zone this interpreter knows about."""
    try:
        ZoneInfo(str(name))
        return True
    except Exception:  # noqa: BLE001 - unknown key, KeyError/TypeError/ValueError
        return False


def canonical_tz(name: str) -> Optional[str]:
    """The IANA spelling of ``name`` (``africa/lagos`` -> ``Africa/Lagos``) for an
    ``Area/City`` zone this interpreter knows, else None."""
    raw = str(name or "").strip()
    if "/" not in raw:
        return None
    if is_valid_tz(raw):
        return raw
    try:
        from zoneinfo import available_timezones
        for known in available_timezones():
            if known.lower() == raw.lower():
                return known
    except Exception:  # noqa: BLE001 - no tz database listing available
        pass
    return None


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_timezone(stored: Optional[str], configured: str = "UTC") -> str:
    """Subscriber preference, then application zone, then safe UTC fallback.

    No location is inferred from Telegram user metadata.
    """
    for candidate in (stored, configured, "UTC"):
        name = str(candidate or "").strip()
        if name and is_valid_tz(name):
            return name
    return "UTC"


def to_local(dt: datetime, tz: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz))
    except Exception:
        return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_reached(dt: datetime, tz: str, at: str) -> bool:
    """True when the wall clock in ``tz`` is at/after ``at`` ("HH:MM").

    Scheduling uses the configured application zone; subscriber preferences
    affect presentation only.
    """
    h, m = parse_hhmm(at)
    local = to_local(dt, tz)
    return (local.hour, local.minute) >= (h, m)


def day_bounds(dt: datetime, tz: str):
    """Start/end (UTC) of the local calendar day containing dt."""
    local = to_local(dt, tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
