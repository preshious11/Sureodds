"""SportsGameOdds quota accounting, budget, cool-down and back-off.

WHAT SGO BILLS (docs: sportsgameodds.com/docs/info/rate-limiting): every OBJECT returned in a response counts against
the monthly allowance - 10 events = 10 objects - with a minimum of 1 object per response (so an empty answer, a
``/leagues`` listing and a ``/account/usage`` call are all billed). The period is a rolling 30 days from first use,
not a calendar month. The Amateur plan is 2,500 objects/month at 10 requests/minute. HTTP requests, retries and
pagination pages are only billed through the objects they return; a ``429`` returns none.

WHY OUR OWN COUNTER DISAGREED: it counted only the events fetched by generation and result lookups. It never counted
``/leagues`` (one object per league), empty answers (minimum 1) or the usage probe; it bucketed by CALENDAR month while
SGO's period rolls, so it reset to zero on the 1st while SGO kept counting; its limit (1,000) was an assumption, not the
plan's 2,500; and every deployment / script / PC has its own database, so each believed it had the whole allowance of
the one shared key. The authoritative number is therefore SGO's own ``/account/usage``; this module keeps an estimate
between probes from the objects it has billed itself (``sgo_usage``) and corrects it on every probe.

BUDGET (derived from what each activity needs, not from round percentages):
  generation   SGO_EVENT_LIMIT (50) events/day x 30 days = 1,500 objects, plus ~5/day of minimum-1 empty answers
               => about 65% of the plan. It gets everything that is left after the reserved shares below, spread over
               the days remaining in the period, so it never starves itself early and scales down smoothly.
  settlement   10% of the plan. Free sources (football-data.org, ESPN, EuroLeague) settle first; SGO is the fallback and
               the only grader for first-half / corners / cards / props.
  diagnostics  4% (manual CLI runs and diagnose scripts).
  reserve      10% never spent on anything but a settlement lookup that has no other source.
Usage levels (50 / 75 / 90 / 100 %) are logged once per period; they switch off non-essential activity progressively
(diagnostics at 50%, SGO as a settlement fallback for things a free source can grade at 75%, manual runs at 90%) and
never reduce generation below what the remaining days can afford.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

PERIOD_DAYS = 30
LEVELS = (50, 75, 90, 100)

#: hours to wait before asking SGO about the same fixture again, by how many times it has already been asked
SETTLEMENT_BACKOFF_H = (0, 2, 6, 12, 24)

REASONS = ("generation", "settlement", "fallback", "diagnostic", "manual", "catalog", "quota_probe")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if s else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Shares:
    reserve: float = 0.10
    settlement: float = 0.10
    diagnostics: float = 0.04


class SgoMeter:
    def __init__(self, db, event_limit: int = 50, fallback_cap: int = 2500, shares: Shares = Shares(),
                 now_fn=_now):
        self.db, self.event_limit, self.fallback_cap, self.shares = db, event_limit, fallback_cap, shares
        self._now = now_fn
        self._mem: Dict[str, str] = {}                       # used when there is no database (a bare unit test)

    # ------------------------------------------------------------------ tiny persistent store
    def _get(self, key: str) -> Optional[str]:
        if self.db is None:
            return self._mem.get(key)
        return self.db.get_kv(f"sgo:{key}")

    def _set(self, key: str, value: str) -> None:
        if self.db is None:
            self._mem[key] = value
        else:
            self.db.set_kv(f"sgo:{key}", value)

    # ------------------------------------------------------------------ attribution
    def record(self, reason: str, endpoint: str, status: str, *, sport: str = "", league: str = "",
               event_id: str = "", attempts: int = 1, objects: int = 0, note: str = "") -> None:
        if self.db is None:
            return
        try:
            self.db.sgo_usage_add(reason, endpoint, status, sport=sport, league=league, event_id=event_id,
                                  http_attempts=attempts, objects_billed=objects, note=note)
        except Exception:  # noqa: BLE001 - bookkeeping must never break a fetch
            log.debug("sgo usage record failed", exc_info=True)

    def _sum(self, where: str, params: tuple = ()) -> int:
        if self.db is None:
            return 0
        rows = self.db.query(f"SELECT COALESCE(SUM(objects_billed),0) AS n FROM sgo_usage WHERE {where}", params)
        return int(rows[0]["n"]) if rows else 0

    def spent_today(self, reason: str) -> int:
        return self._sum("day=? AND reason=?", (self._now().strftime("%Y-%m-%d"), reason))

    def spent_since(self, since: Optional[datetime], reasons: Tuple[str, ...] = ()) -> int:
        clause, params = "ts>=?", [_iso(since) if since else "0000"]
        if reasons:
            clause += " AND reason IN (%s)" % ",".join("?" for _ in reasons)
            params += list(reasons)
        return self._sum(clause, tuple(params))

    # ------------------------------------------------------------------ the authoritative number + our estimate
    def observe(self, used: int, cap: int) -> None:
        """A fresh reading from /account/usage: keep it, notice a period reset, raise level warnings."""
        now = self._now()
        last = self._get("probe_used")
        if last is not None and used < int(last):
            self._set("period_start", _iso(now))                           # the rolling period turned over
            self._set("warned_level", "0")
            log.info("SGO: usage went %s -> %d: a new quota period started", last, used)
        self._set("probe_used", str(used))
        self._set("probe_cap", str(cap))
        self._set("probe_at", _iso(now))
        self._warn(used, cap)

    def reading(self) -> Optional[Tuple[int, int]]:
        """(estimated used, cap): the last probe plus what we have billed since, or None before any probe."""
        used, cap, at = self._get("probe_used"), self._get("probe_cap"), _parse(self._get("probe_at"))
        if used is None or cap is None or at is None:
            return None
        # the probe's own billed object is already inside SGO's number, so it is not added again
        return int(used) + self.spent_since(at, ("generation", "settlement", "fallback", "diagnostic", "manual",
                                                  "catalog")), int(cap)

    def probe_due(self) -> bool:
        """Refresh from /account/usage rarely (the call is itself billed): every 12 h normally, every 3 h from 75 %."""
        at = _parse(self._get("probe_at"))
        if at is None:
            return True
        r = self.reading()
        hours = 3 if r and r[0] >= 0.75 * r[1] else 12
        return self._now() - at >= timedelta(hours=hours)

    @staticmethod
    def level(used: int, cap: int) -> int:
        pct = 100.0 * used / cap if cap else 0.0
        return max([0] + [lv for lv in LEVELS if pct >= lv])

    def _warn(self, used: int, cap: int) -> None:
        lv = self.level(used, cap)
        if lv and lv > int(self._get("warned_level") or 0):
            self._set("warned_level", str(lv))
            log.warning("SGO quota at %d%% (%d of %d objects this period)", lv, used, cap)

    def days_left(self) -> int:
        start = _parse(self._get("period_start"))
        if start is None:
            return PERIOD_DAYS                                   # unknown: assume the whole period is still ahead (cautious)
        return max(1, PERIOD_DAYS - (self._now() - start).days)

    # ------------------------------------------------------------------ what may be spent
    def exhausted(self) -> bool:
        r = self.reading()
        return r is not None and r[0] >= r[1] - 25

    def generation_allowance(self) -> Optional[int]:
        """Events generation may fetch today, or None when usage is unknown (callers keep their own limit)."""
        r = self.reading()
        if r is None:
            return None
        used, cap = r
        held_back = cap * (self.shares.reserve + self.shares.settlement + self.shares.diagnostics)
        # today's share is fixed from where the day STARTED (so spending it does not shrink it a second time), and what
        # generation has already taken today is then taken off it
        today = self.spent_today("generation")
        per_day = (cap - (used - today) - held_back) / self.days_left()
        return max(0, min(self.event_limit, int(math.floor(per_day))) - today)

    def allows(self, reason: str, origin: str = "scheduled", needs_sgo: bool = True) -> bool:
        """May a request with this purpose go out now? ``needs_sgo``: for settlement, False means a free source could
        also have graded it (SGO is then the first thing to stop as usage climbs)."""
        r = self.reading()
        if r is None:
            return True
        used, cap = r
        lv = self.level(used, cap)
        if lv >= 100 or used >= cap - 25:
            return False
        if origin == "diagnostic" and lv >= 50:
            return False
        if origin == "manual" and lv >= 90:
            return False
        if reason in ("settlement", "fallback"):
            if not needs_sgo and lv >= 75:
                return False
            if needs_sgo:
                return True                                      # may dip into the reserve: nothing else can grade it
            return self.spent_since(_parse(self._get("period_start")), ("settlement", "fallback")) \
                < self.shares.settlement * cap
        if reason == "generation":
            return (self.generation_allowance() or 0) > 0
        return True

    # ------------------------------------------------------------------ cool-down after a 429
    def in_cooldown(self) -> bool:
        until = _parse(self._get("cooldown_until"))
        return until is not None and self._now() < until

    def cooldown_until(self) -> Optional[datetime]:
        until = _parse(self._get("cooldown_until"))
        return until if until and self._now() < until else None

    def rate_limited(self, quota_gone: bool) -> timedelta:
        """Register a 429 and return the cool-down. Quota gone: wait until the usage probe is due again (6 h); a
        per-minute limit: 2 min, then 10, then 60 on consecutive 429s. A success resets the ladder."""
        if quota_gone:
            wait = timedelta(hours=6)
        else:
            n = int(self._get("rl_count") or 0)
            wait = timedelta(minutes=(2, 10, 60)[min(n, 2)])
            self._set("rl_count", str(n + 1))
        self._set("cooldown_until", _iso(self._now() + wait))
        log.warning("SGO: rate limited (%s) - pausing all SGO requests for %s",
                    "monthly quota used up" if quota_gone else "request limit", wait)
        return wait

    def succeeded(self) -> None:
        if self._get("rl_count") not in (None, "0"):
            self._set("rl_count", "0")

    # ------------------------------------------------------------------ settlement back-off
    def settlement_due(self, event_key: str) -> bool:
        """A fixture already asked about is asked again only after 2 h, 6 h, 12 h, then daily - not on every run."""
        if self.db is None or not event_key:
            return True
        rows = self.db.query("SELECT COUNT(*) AS n, MAX(ts) AS last FROM sgo_usage WHERE event_id=? "
                             "AND reason IN ('settlement','fallback') AND status='ok'", (event_key,))
        n, last = int(rows[0]["n"]), _parse(rows[0]["last"])
        if n == 0 or last is None:
            return True
        wait_h = SETTLEMENT_BACKOFF_H[min(n, len(SETTLEMENT_BACKOFF_H) - 1)]
        return self._now() - last >= timedelta(hours=wait_h)

    # ------------------------------------------------------------------ reporting (admin / CLI only)
    def snapshot(self) -> Dict:
        r = self.reading()
        out = {"estimated_used": r[0] if r else None, "cap": r[1] if r else None,
               "level": self.level(*r) if r else None, "days_left": self.days_left(),
               "generation_allowance_today": self.generation_allowance(),
               "cooldown_until": _iso(self.cooldown_until()) if self.cooldown_until() else None,
               "period_start": self._get("period_start"), "probe_at": self._get("probe_at")}
        return out


def usage_report(db, days: int = 30) -> Dict:
    """Operator view of SGO consumption, read from the local database only (no request is made): billed objects, HTTP
    attempts and requests per reason, today and over ``days``, plus the retry overhead. Contains no key, e-mail or id."""
    since = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    today = _now().strftime("%Y-%m-%d")
    by_reason: Dict[str, Dict[str, int]] = {}
    for r in db.sgo_usage_summary(since):
        slot = by_reason.setdefault(r["reason"], {"requests": 0, "http_attempts": 0, "objects": 0,
                                                  "today_objects": 0, "failed": 0})
        slot["requests"] += int(r["requests"] or 0)
        slot["http_attempts"] += int(r["attempts"] or 0)
        slot["objects"] += int(r["objects"] or 0)
        if r["status"] != "ok":
            slot["failed"] += int(r["requests"] or 0)
        if r["day"] == today:
            slot["today_objects"] += int(r["objects"] or 0)
    meter = SgoMeter(db)
    snap = meter.snapshot()
    snap["retry_requests"] = sum(max(0, v["http_attempts"] - v["requests"]) for v in by_reason.values())
    snap["http_requests"] = sum(v["http_attempts"] for v in by_reason.values())
    snap["entities_consumed"] = sum(v["objects"] for v in by_reason.values())
    snap["by_reason"] = by_reason
    return snap


def usage_lines(db, days: int = 30) -> list:
    """usage_report as plain text lines (CLI and the admin-only /lastrun)."""
    rep = usage_report(db, days)
    used, cap = rep["estimated_used"], rep["cap"]
    lines = [f"quota: {used if used is not None else '?'} / {cap if cap is not None else '?'} objects"
             + (f" ({rep['level']}% level)" if rep.get("level") else "")
             + f" · generation allowance today: {rep['generation_allowance_today'] if rep['generation_allowance_today'] is not None else 'n/a'}"
             + f" · days left: {rep['days_left']}",
             f"last {days}d: sgo_http_requests={rep['http_requests']} sgo_entities_consumed={rep['entities_consumed']} "
             f"sgo_retry_requests={rep['retry_requests']}"]
    if rep.get("cooldown_until"):
        lines.append(f"cooling down until {rep['cooldown_until']}")
    for reason, v in sorted(rep["by_reason"].items()):
        lines.append(f"{reason}: {v['requests']} req · {v['http_attempts']} http · {v['objects']} objects "
                     f"({v['today_objects']} today) · {v['failed']} failed")
    return lines
