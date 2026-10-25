"""Weekly Pro value shadow report, checked against the go-live criteria in PRO_VALUE_GO_LIVE.md.

Read-only: the shadow DB is opened with mode=ro, and nothing here changes any setting or any output. The report is an
input to the owner's decision, not a decision."""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import statistics
from typing import Dict, Optional

MIN_PICKS = 40
#: "Regularly" = Bet of the Day's top candidate lost to Pro on more than 1 run in 4.
BOTD_TOP_LOST_CEILING = 0.25
MIN_DAYS_AFTER_RESET = 14
BEAT_CLOSE_SHARE = 0.5
DAY = 86400


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def weekly_report(db_path: str, now_ts: int, *, reset_ts: int, remaining: Optional[int] = None,
                  max_price_age_min: int = 30, week_days: int = 7) -> Dict:
    con = _connect_ro(db_path)
    try:
        since = now_ts - week_days * DAY
        picks = [dict(r) for r in con.execute(
            "SELECT run_at, kickoff_ts, sharp_fetched_at, closing_captured_at, clv FROM pro_value_rows "
            "WHERE status='selected'")]
        clv = [p["clv"] for p in picks if p["clv"] is not None]
        week_picks = [p for p in picks if p["run_at"] >= since]
        week_clv = [p["clv"] for p in week_picks if p["clv"] is not None]
        missed = sum(1 for p in picks if p["closing_captured_at"] is None and p["kickoff_ts"] < now_ts)
        stale = sum(1 for p in picks if p["sharp_fetched_at"] is not None
                    and p["run_at"] - p["sharp_fetched_at"] > max_price_age_min * 60)
        since_day = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%d")
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='closing_spend'").fetchone():
            spend_total = con.execute("SELECT COALESCE(SUM(calls),0) FROM closing_spend").fetchone()[0]
            spend_week = con.execute("SELECT COALESCE(SUM(calls),0) FROM closing_spend WHERE day>=?",
                                     (since_day,)).fetchone()[0]
        else:  # a database from before closing captures existed has no spend yet
            spend_total = spend_week = 0
        botd_total, botd_lost = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(lost_to_pro),0) FROM pro_value_botd_compare").fetchone()
        # Top candidate of each run = the first row written for that run (rows are written in rank order).
        top_rows = con.execute(
            "SELECT lost_to_pro FROM pro_value_botd_compare WHERE rowid IN "
            "(SELECT MIN(rowid) FROM pro_value_botd_compare GROUP BY run_id)").fetchall()
        mismatches = con.execute("SELECT COUNT(*) FROM pro_value_mismatches").fetchone()[0]
        mismatches_week = con.execute("SELECT COUNT(*) FROM pro_value_mismatches WHERE logged_at>=?",
                                      (since,)).fetchone()[0]
        mismatch_kinds = dict(con.execute(
            "SELECT kind, COUNT(*) FROM pro_value_mismatches GROUP BY kind").fetchall())
    finally:
        con.close()

    reset_plus = reset_ts + MIN_DAYS_AFTER_RESET * DAY
    top_lost_share = (sum(r[0] for r in top_rows) / len(top_rows)) if top_rows else None
    avg = statistics.fmean(clv) if clv else None
    beat = (sum(1 for c in clv if c > 0) / len(clv)) if clv else None
    criteria = {
        "sample_and_time": {
            "met": len(picks) >= MIN_PICKS and now_ts >= reset_plus,
            "picks": len(picks), "need_picks": MIN_PICKS,
            "earliest_date": dt.datetime.fromtimestamp(reset_plus, dt.timezone.utc).strftime("%Y-%m-%d"),
        },
        "average_clv_above_zero": {"met": avg is not None and avg > 0, "average": avg},
        "more_than_half_beat_close": {"met": beat is not None and beat > BEAT_CLOSE_SHARE, "share": beat},
        "data_quality": {"met": missed == 0 and stale == 0 and mismatches == 0, "missed_captures": missed,
                         "stale_quotes": stale, "mismatches": mismatches},
        "botd_not_regularly_lost_to_pro": {
            "met": None if top_lost_share is None else top_lost_share <= BOTD_TOP_LOST_CEILING,
            "top_lost_share": top_lost_share, "runs": len(top_rows), "ceiling": BOTD_TOP_LOST_CEILING,
            "lost_share_all": (botd_lost / botd_total) if botd_total else None},
    }
    return {
        "generated_at_utc": dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "picks_total": len(picks), "picks_week": len(week_picks),
        "clv_average": avg, "clv_median": statistics.median(clv) if clv else None,
        "clv_week_average": statistics.fmean(week_clv) if week_clv else None,
        "beat_close_share": beat,
        "credits_spent_week": spend_week, "credits_spent_total": spend_total,
        "credits_remaining": remaining,
        "botd_candidates_compared": botd_total, "botd_lost_to_pro": botd_lost,
        "criteria": criteria,
        "mismatches_total": mismatches, "mismatches_week": mismatches_week, "mismatch_kinds": mismatch_kinds,
    }


def render_markdown(report: Dict) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v * 100:.2f}%"
    c = report["criteria"]
    def mark(m):
        return "owner review" if m is None else ("met" if m else "not met")
    lines = [
        f"# Pro value shadow report ({report['generated_at_utc']} UTC)",
        "",
        f"- Picks: {report['picks_total']} total, {report['picks_week']} this week",
        f"- CLV: average {pct(report['clv_average'])}, median {pct(report['clv_median'])}, "
        f"this week {pct(report['clv_week_average'])}",
        f"- Beat close: {pct(report['beat_close_share'])} of picks",
        f"- Credits: {report['credits_spent_week']} this week, {report['credits_spent_total']} total; "
        f"remaining {report['credits_remaining'] if report['credits_remaining'] is not None else 'not given'}",
        f"- Bet of the Day: {report['botd_lost_to_pro']} of {report['botd_candidates_compared']} candidates "
        f"lost to Pro ({pct(c['botd_not_regularly_lost_to_pro']['lost_share_all'])})",
        "",
        "## Go-live criteria",
        f"- Sample and time: {mark(c['sample_and_time']['met'])} "
        f"({c['sample_and_time']['picks']}/{MIN_PICKS} picks; earliest {c['sample_and_time']['earliest_date']})",
        f"- Average CLV above 0%: {mark(c['average_clv_above_zero']['met'])}",
        f"- More than half beat close: {mark(c['more_than_half_beat_close']['met'])}",
        f"- Data quality: {mark(c['data_quality']['met'])} (missed captures {c['data_quality']['missed_captures']}, "
        f"stale quotes {c['data_quality']['stale_quotes']}, mismatches {c['data_quality']['mismatches']})",
        f"- Bet of the Day top candidate lost to Pro on at most 1 run in 4: {mark(c['botd_not_regularly_lost_to_pro']['met'])} "
        f"({pct(c['botd_not_regularly_lost_to_pro']['top_lost_share'])} of {c['botd_not_regularly_lost_to_pro']['runs']} runs)",
        "",
        "This report does not change any setting. Go-live is the owner's decision.",
    ]
    return "\n".join(lines) + "\n"
