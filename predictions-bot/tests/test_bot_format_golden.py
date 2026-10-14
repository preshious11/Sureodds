"""Shared golden fixture for the staging-Worker Telegram UI port.

workers/tests/fixtures/golden_pick_line.json is rendered by BOTH the real
production formatter (here, via predbot.bot.format.pick_lines) and the
Worker's TypeScript port (workers/tests/format.test.mjs). Both must produce
byte-identical output for the fields the current D1 schema actually carries
(no separate country column, hence no "Country • League" prefix exercised
here — see workers/PRODUCTION_PARITY.md for that documented gap).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from predbot.bot.format import PickLine, clock_label, leg_state, pick_lines

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "workers" / "tests" / "fixtures" / "golden_pick_line.json")
    .read_text(encoding="utf-8")
)


def test_pick_lines_matches_the_shared_golden_fixture():
    i = FIXTURE["input"]
    start = datetime.strptime(i["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = datetime.strptime(i["now_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    line = PickLine(
        sport=i["sport_label"], match=f"{i['home']} vs {i['away']}", outcome=i["outcome"],
        league=i["league"], confidence=i["confidence"], when=clock_label(start, "Africa/Lagos"),
        price=i["price"], country="", market_type="", state=leg_state(start, i["sport_key"], now),
        start=start, sport_key=i["sport_key"],
    )
    assert pick_lines(line) == FIXTURE["expected_lines"]
