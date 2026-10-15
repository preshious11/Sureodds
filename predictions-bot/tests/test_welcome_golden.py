"""Shared golden fixture for the /start welcome screen (SureOdds UI-parity
audit, 2026-10-15).

workers/tests/fixtures/golden_welcome.json is rendered by BOTH the real
production formatter (here, via predbot.bot.callbacks.welcome_message) and
the Worker's TypeScript port (workers/tests/index.test.mjs). Both must
produce byte-identical output, including HTML-escaping of a first name
containing '&', '<', '>'.
"""
import json
from pathlib import Path

from predbot.bot.callbacks import welcome_message

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "workers" / "tests" / "fixtures" / "golden_welcome.json")
    .read_text(encoding="utf-8")
)


def test_welcome_message_matches_the_shared_golden_fixture():
    for case in FIXTURE["cases"]:
        assert welcome_message(case["first_name"]) == case["expected"], case["first_name"]
