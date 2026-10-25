"""PRO_VALUE_MODE: the single switch from shadow to publish for Pro value picks.

shadow (default): nothing is published and live Bet of the Day ignores Pro. This is the current behaviour.
publish: the Pro value picks are published, and live Bet of the Day excludes every event Pro has picked.

``publish_plan`` is pure: it decides what would be published and which events Bet of the Day must drop. In shadow it
returns nothing at all, so the caller sees exactly the pre-switch behaviour. Wiring the publish path and the Bet of the
Day hand-off is separate work and is not connected here."""
from __future__ import annotations

from typing import Dict, Iterable, List, Set

SHADOW = "shadow"
PUBLISH = "publish"
MODES = (SHADOW, PUBLISH)


def parse_mode(value) -> str:
    mode = ("" if value is None else str(value)).strip().lower() or SHADOW
    if mode not in MODES:
        raise ValueError("PRO_VALUE_MODE must be 'shadow' or 'publish'")
    return mode


def publish_plan(mode, pro_picks: Iterable[Dict]) -> Dict:
    """What the switch decides for one day.

    ``publish``: the Pro picks, and the match keys Bet of the Day must exclude.
    ``shadow``: nothing published and nothing excluded, whatever the picks are.
    ``no_pick_message`` is True only in publish mode with an empty Pro selection (the "No Pro pick today" message)."""
    picks: List[Dict] = list(pro_picks)
    if parse_mode(mode) == SHADOW:
        return {"mode": SHADOW, "publish": [], "botd_exclude": set(), "no_pick_message": False}
    exclude: Set[str] = {p["match_key"] for p in picks}
    return {"mode": PUBLISH, "publish": picks, "botd_exclude": exclude, "no_pick_message": not picks}
