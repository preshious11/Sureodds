"""Shadow run: shared OddsAPI raw data + SportyBet 1X2 in one run -> Pro value selection -> stored shadow results.

Nothing here publishes. Bet of the Day is compared with Pro's selection in the shadow results only; live Bet of
the Day output is never changed by this module."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from predbot.pro_value import botd_archive
from predbot.pro_value.matching import match_events
from predbot.pro_value.oddsapi_raw import load_raw_dir
from predbot.pro_value.select import Gates, select_pro
from predbot.util.timeutil import iso


def run_shadow(cfg, *, raw_dir: str, sporty_events: List[Dict], store, now_ts: int,
               botd: Optional[List[Dict]] = None, archive_rows: Optional[List[Dict]] = None,
               run_id: Optional[str] = None) -> Dict:
    """``sporty_events`` is the SportyBet 1X2 list fetched in THIS run. ``botd`` is Bet of the Day's candidates in
    rank order, each {match_key, outcome}; ``archive_rows`` (the run's candidate archive, read-only) is ranked into
    Bet of the Day with and without the Pro exclusion. Both are only used for the shadow comparison."""
    if not cfg.pro_value_shadow_enabled:
        return {"status": "disabled"}
    run_id = run_id or uuid.uuid4().hex[:12]
    gates = Gates.from_config(cfg)
    odds_events = load_raw_dir(raw_dir)
    matched_pairs = match_events(odds_events, sporty_events, window_min=cfg.pro_value_match_window_min)
    matched = []
    for pair in matched_pairs:
        # Matched events keep their own bookmaker dictionary; the sharp fetch time is the raw file's time.
        matched.append({"odds": pair["odds"], "sporty": pair["sporty"]})
    selection = select_pro(matched, gates)
    rows = selection.picks + selection.near_misses + selection.rejected
    store.insert_rows(run_id, now_ts, [r for r in rows if r["status"] in ("selected", "near_miss", "needs_review", "not_top")])
    pro_keys = {p["match_key"] for p in selection.picks}
    top_excluded = None
    if archive_rows is not None:
        now_utc = datetime.fromtimestamp(now_ts, timezone.utc)
        botd = botd_archive.bet_of_the_day(archive_rows, cfg, iso(now_utc))
        top_excluded = [b["match_key"] for b in botd_archive.bet_of_the_day(
            archive_rows, cfg, iso(now_utc), exclude=pro_keys)]
    botd_report = _botd_comparison(botd or [], pro_keys, store, run_id)
    if top_excluded is not None:
        botd_report["top_with_pro_excluded"] = top_excluded
    return {"status": "ok", "run_id": run_id, "odds_events": len(odds_events), "matched_events": len(matched),
            "sporty_events": len(sporty_events), "picks": len(selection.picks),
            "near_misses": len(selection.near_misses), "needs_review": sum(1 for r in selection.rejected if r["status"] == "needs_review"),
            "no_pick_reason": selection.no_pick_reason, "botd": botd_report}


def _botd_comparison(botd: List[Dict], pro_keys: set, store, run_id: str) -> Dict:
    """How often Bet of the Day would have lost a candidate to Pro, and the substitute it would have shown."""
    lost, substitutes = [], []
    unused = [b for b in botd if b["match_key"] not in pro_keys]
    for b in botd:
        is_lost = b["match_key"] in pro_keys
        substitute = None
        if is_lost and unused:
            substitute = unused.pop(0)["match_key"]
            substitutes.append(substitute)
        if is_lost:
            lost.append(b["match_key"])
        store.insert_botd_compare(run_id, b["match_key"], is_lost, substitute)
    return {"candidates": len(botd), "lost_to_pro": len(lost), "substitutes": substitutes}
