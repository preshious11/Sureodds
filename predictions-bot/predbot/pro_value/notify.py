"""Private test message of the shadow Pro picks to the owner's admin chat. Shadow only: nothing is published.

The only destination is the admin chat id (the first TELEGRAM_ADMIN_IDS entry in workers/wrangler.toml, the same
value the owner-notice step reads). Never the channel, never subscribers. Read-only on the shadow DB."""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

TELEGRAM_API = "https://api.telegram.org"
MAX_CHARS = 3900
NEAR_MISS_LIMIT = 3
LAGOS = dt.timezone(dt.timedelta(hours=1))  # Africa/Lagos has no DST


def admin_chat_id(wrangler_toml: str) -> Optional[str]:
    """First admin id from the Worker config, or None when it is not set."""
    m = re.search(r'^TELEGRAM_ADMIN_IDS\s*=\s*"([^"]*)"', wrangler_toml, re.M)
    if not m:
        return None
    first = next((s.strip() for s in m.group(1).split(",") if s.strip()), "")
    return first or None


def _connect_ro(db_path: str) -> sqlite3.Connection:
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_run(db_path: str, run_id: str) -> Tuple[List[Dict], List[Dict]]:
    """(selected picks, top near misses) stored by this run."""
    con = _connect_ro(db_path)
    try:
        picks = [dict(r) for r in con.execute(
            "SELECT * FROM pro_value_rows WHERE run_id=? AND status='selected' ORDER BY kickoff_ts", (run_id,))]
        near = [dict(r) for r in con.execute(
            "SELECT * FROM pro_value_rows WHERE run_id=? AND status='near_miss' "
            "ORDER BY edge DESC, kickoff_ts LIMIT ?", (run_id, NEAR_MISS_LIMIT))]
    finally:
        con.close()
    return picks, near


def _kickoff(ts: int) -> str:
    utc = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
    return f"{utc:%a %d %b %H:%M} UTC ({utc.astimezone(LAGOS):%H:%M} Lagos)"


def _fair_price(prob: Optional[float]) -> str:
    return "n/a" if not prob else f"{1.0 / prob:.2f}"


def _edge(edge: Optional[float]) -> str:
    return "n/a" if edge is None else f"{edge * 100:+.2f}%"


def _books(row: Dict) -> str:
    books = [b for b in (row.get("fair_book"), row.get("second_book")) if b]
    return ", ".join(books) if books else "n/a"


def _pick_line(p: Dict) -> str:
    return "\n".join([
        f"- {p['home']} v {p['away']}",
        f"  Kickoff: {_kickoff(p['kickoff_ts'])}",
        f"  Outcome: {p['outcome']}",
        f"  SportyBet price: {p['sporty_odds']:.2f}",
        f"  Sharp fair price: {_fair_price(p.get('fair_probability'))}",
        f"  Edge: {_edge(p.get('edge'))}",
        f"  Books used: {_books(p)}",
    ])


def _near_line(n: Dict) -> str:
    return (f"- {n['home']} v {n['away']} ({n['outcome']}): price {n['sporty_odds']:.2f}, "
            f"edge {_edge(n.get('edge'))}, books {_books(n)}, reason {n.get('reason') or 'n/a'}")


def build_message(result: Optional[Dict], picks: List[Dict], near: List[Dict]) -> str:
    """The admin message. ``result`` is the shadow run's JSON (None when the run produced no result)."""
    if result is None:
        return "Pro shadow (private test): the shadow run produced no result today. Nothing was published."
    head = (f"Pro shadow (private test, not published). Run {result.get('run_id', 'n/a')}: "
            f"{result.get('matched_events', 0)} matched events, {result.get('picks', 0)} picks, "
            f"{result.get('near_misses', 0)} near misses.")
    if not picks:
        lines = [head, "", "No Pro pick today", "", "Top near misses:"]
        lines += [_near_line(n) for n in near] or ["- none"]
    else:
        lines = [head, ""] + [_pick_line(p) for p in picks]
    return "\n".join(lines)


def split_for_telegram(text: str, limit: int = MAX_CHARS) -> List[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_to_admin(token: str, admin_id: str, text: str, post) -> int:
    """Send to the admin chat only. ``post`` is requests.post (injected for tests). Returns the chunks sent."""
    if not token or not admin_id:
        raise ValueError("bot token and admin chat id are both required")
    sent = 0
    for chunk in split_for_telegram(text):
        resp = post(f"{TELEGRAM_API}/bot{token}/sendMessage",
                    data={"chat_id": admin_id, "text": chunk, "disable_web_page_preview": "true"}, timeout=20)
        if getattr(resp, "status_code", 500) != 200:
            raise RuntimeError(f"telegram returned HTTP {getattr(resp, 'status_code', 'unknown')}")
        sent += 1
    return sent
