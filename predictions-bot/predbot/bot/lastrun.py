"""/lastrun - the most recent generation cycle, formatted for Telegram (admin only).

Everything here was recorded at generation time (snapshot diagnostics, the
per-cycle API accounting, the Odds API quota block, per-day cache counters), so
it answers "what happened at 09:00?" without needing the process logs. Same
content as the log summary, plus both slates exactly as a subscriber's card
renders them.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from predbot.bot.broadcast import picks_text
from predbot.bot.format import kickoff_label
from predbot.bot.telegram import esc
from predbot.cache.models import DailySnapshot
from predbot.util.pipeline_report import pipeline_lines
from predbot.util.timeutil import parse_hhmm, parse_iso, to_local, utcnow


def latest_snapshot(db) -> Optional[Tuple[DailySnapshot, Dict]]:
    row = db.latest_prediction_cache_row()
    if not row:
        return None
    try:
        return DailySnapshot.from_dict(json.loads(row["payload_json"])), row
    except (ValueError, TypeError, KeyError):
        return None


def _kv(d: Optional[Dict[str, Any]]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted((d or {}).items())) or "none"


def _events(report: Dict) -> str:
    counts = report.get("events") or {}
    return f"{sum(counts.values())} events" + (f" ({_kv(counts)})" if counts else "")


def _provider_line(name: str, report: Dict, usage: Dict) -> str:
    calls = usage.get(name)
    spent = ""
    if isinstance(calls, dict):
        limit = calls.get("limit") or "?"
        spent = f" · +{calls.get('cycle', 0)} calls ({calls.get('today', 0)}/{limit} today)"
    status = report.get("status", "?")
    if report.get("outcome") and report.get("outcome") != "success":
        status += f"/{report['outcome']}"
    cache = f" · cache {esc(report['cache'])}" if report.get('cache') else ""
    if report.get('refresh_reason'):
        cache += f" ({esc(report['refresh_reason'])})"
    return f"• <b>{esc(name)}</b> — {esc(status)} · {esc(_events(report))}{esc(spent)}{cache}"


def _feed_lines(providers: Dict) -> List[str]:
    """Per-feed SharpAPI paging: requests, rows, matches, rows per match, why it stopped."""
    report = providers.get("sharpapi") or {}
    fetch = report.get("fetch") or {}
    lines: List[str] = []
    req = report.get("requests")
    if req:
        lines.append(f"Requests: <b>{req.get('spent')}</b> of budget <b>{req.get('budget') if req.get('budget') is not None else '∞'}</b>")
    for league, c in fetch.items():
        if not isinstance(c, dict) or "requests" not in c:
            continue
        lines.append(f"• {esc(league)}: {c['requests']} req · {c.get('rows', 0)} rows · "
                     f"{c.get('matches_in_window', 0)}/{c.get('matches_seen', 0)} matches in window · "
                     f"{c.get('rows_per_match', 0)} rows/match · stop: {esc(c.get('stop_reason') or '?')}"
                     + (" · <b>capped</b>" if c.get("capped") else ""))
    return lines


def _quota_lines(db, gen: Dict) -> List[str]:
    report = (gen.get("providers") or {}).get("oddsapi") or {}
    quota = report.get("quota") or {}
    remaining = quota.get("requests_remaining", db.get_kv("oddsapi:requests_remaining"))
    used = quota.get("requests_used", db.get_kv("oddsapi:requests_used"))
    lines = [f"• remaining <b>{esc(remaining if remaining not in (None, '') else '?')}</b> · "
             f"used <b>{esc(used if used not in (None, '') else '?')}</b>"
             + (f" · reserve {quota['reserve_credits']}" if quota.get("reserve_credits") is not None else "")]
    if quota.get("refused_to_spend"):
        lines.append(f"• ⚠️ refused to spend: {esc(quota.get('refusal') or 'quota gate')}")
    elif quota:
        lines.append("• no refusal - the quota gate allowed the spend")
    if report:
        lines.append(f"• {esc(report.get('status', '?'))} · {esc(_events(report))}")
    return lines


def _sgo_lines(db) -> List[str]:
    try:
        from predbot.providers.sgo_meter import usage_lines
        return [f"• {esc(line)}" for line in usage_lines(db)]
    except Exception:                                   # a report must never break /lastrun
        return ["• unavailable"]


def _funnel(gen: Dict) -> List[str]:
    lines = [esc(l) for l in pipeline_lines(gen)]
    if gen.get("merged_events") is not None:
        lines.append(f"Merged across providers: <b>{gen['merged_events']}</b> events | by league: "
                     f"{esc(_kv(gen.get('merged_pairs_by_league')))}")
        for p in (gen.get("merged_pairs") or [])[:12]:
            lines.append(f"• {esc(p['kept'])} ⇄ {esc(p['merged'])}" + (" (sides swapped)" if p.get("swapped") else ""))
    if gen.get("timings"):
        lines.append("Timings (wall/cpu s): " + esc(", ".join(f"{k} {v['wall']:.2f}/{v['cpu']:.2f}"
                                                            for k, v in gen["timings"].items())))
    skipped = gen.get("leagues_skipped_no_results") or {}
    lines.append("Leagues not fetched (no results path): "
                 + (esc("; ".join(f"{p}: {', '.join(v)}" for p, v in sorted(skipped.items()))) if skipped else "none"))
    return lines


def _price_source(pick) -> str:
    """Where a pick's price came from, e.g. "median of 6 books" / "Bet365" (operator view; not on the card)."""
    book = str(getattr(pick, "bookmaker", "") or "").strip()
    n = int(getattr(pick, "n_books", 0) or 0)
    if not book:
        return "unknown"
    if book in ("median", "consensus"):
        return f"{book} of {n} books"
    return book.replace("_", " ")


def _price_sources(snapshot) -> List[str]:
    picks = list(snapshot.all_predictions)
    return [f"• {esc(p.label)} @ {p.price:.2f} — {esc(_price_source(p))}" for p in picks] or ["• none"]


def _product(picks) -> float:
    return math.prod(p.price for p in picks) if picks else 0.0


def lastrun_text(db, cfg, now: Optional[datetime] = None) -> str:
    """One Telegram message (the sender splits it) describing the last cycle."""
    found = latest_snapshot(db)
    if not found:
        return "🛠 <b>Last run</b>\nNo generation has been recorded yet."
    snapshot, _row = found
    gen = snapshot.generation or {}
    tz = cfg.timezone
    at = parse_iso(snapshot.generated_at)
    cycle = gen.get("cycle") or {}
    hits = db.get_kv(f"cache:hit:{snapshot.date}") or "0"
    misses = db.get_kv(f"cache:miss:{snapshot.date}") or "0"
    usage = gen.get("api_usage") or {}

    out = [f"🛠 <b>Last run</b> · {esc(snapshot.date)} ({esc(tz)})",
           f"Generated {esc(kickoff_label(at, tz))} · status <b>{esc(snapshot.status)}</b>",
           f"Trigger: {esc(cycle.get('trigger', 'n/a'))} · cache: <b>{esc(cycle.get('cache', 'n/a'))}</b> "
           f"(reads that day: {esc(hits)} hit / {esc(misses)} miss)", "",
           "<b>Providers</b>"]
    providers = gen.get("providers") or {}
    out.append(f"Pipeline v{esc(gen.get('pipeline_version', '?'))} · revision {esc(gen.get('runtime_revision', 'unknown'))}")
    out += [_provider_line(n, r, usage) for n, r in sorted(providers.items())] or ["• none recorded"]
    parse = [f"{esc(n)}: {esc(_kv(r.get('rejections')))}" for n, r in sorted(providers.items())
             if isinstance(r, dict) and r.get("rejections")]
    if parse:
        out += ["", "<b>Parse rejections</b>"] + [f"• {p}" for p in parse]
    feed = _feed_lines(providers)
    if feed:
        out += ["", "<b>SharpAPI feeds</b>"] + feed
    out += ["", "<b>Odds API</b>"] + _quota_lines(db, gen)
    out += ["", "<b>SportsGameOdds usage</b> (this database, last 30 days)"] + _sgo_lines(db)
    out += ["", "<b>Funnel</b>"] + _funnel(gen)
    out += ["", "<b>Price sources</b> (not shown on the card)"] + _price_sources(snapshot)
    fixtures = len({p.fixture_id or (p.provider, p.provider_event_id) for p in snapshot.all_predictions})
    out += ["", f"<b>Collections</b>: All {len(snapshot.all_predictions)} predictions on {fixtures} fixtures | "
                f"Top {len(snapshot.top_picks)} legs · combined {_product(snapshot.top_picks):.2f} | "
                f"Daily {len(snapshot.daily_picks)} picks · combined {_product(snapshot.daily_picks):.2f}"]

    # Both slates exactly as a subscriber's card renders them at DELIVERY_TIME in the bot's zone.
    d_hour, d_minute = parse_hhmm(cfg.delivery_time, "09:30")
    sent_at = to_local(at, tz).replace(hour=d_hour, minute=d_minute, second=0, microsecond=0) if at else None
    card = picks_text(snapshot, tz, now=sent_at) if snapshot.total_picks() else ""
    out += ["", f"<b>Slates as sent</b> (rendered for {esc(tz)} at {d_hour:02d}:{d_minute:02d}):",
            card or "No picks in this snapshot."]
    return "\n".join(out)
