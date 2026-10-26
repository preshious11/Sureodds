"""The shadow pick message goes only to the admin chat, carries every requested field, and never leaks the token."""
import sqlite3
from types import SimpleNamespace

import pytest

from predbot.pro_value import notify
from predbot.pro_value.store import ShadowStore

TOKEN = "123456789:AAFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEXY"
NOW = 1_800_000_000
KICK = NOW + 3 * 3600
WRANGLER = 'name = "sureodds-worker"\nTELEGRAM_ADMIN_IDS = "8606823551,111"\nTELEGRAM_CHAT_ID = "-100"\n'


def _row(**kw):
    base = {"match_key": "arsenal|chelsea|1", "league": "soccer_epl", "home": "Arsenal", "away": "Chelsea",
            "kickoff_ts": KICK, "outcome": "away", "sporty_odds": 3.98, "sporty_fetched_at": NOW,
            "fair_probability": 0.2513, "fair_book": "pinnacle", "sharp_fetched_at": NOW, "edge": 0.0317,
            "second_book": "matchbook", "second_edge": 0.02, "status": "selected", "reason": None, "rank": 1}
    base.update(kw)
    return base


def test_admin_chat_is_the_first_admin_id_only():
    assert notify.admin_chat_id(WRANGLER) == "8606823551"
    assert notify.admin_chat_id('TELEGRAM_CHAT_ID = "-100"\n') is None


def test_pick_message_has_every_requested_field():
    picks = [_row()]
    text = notify.build_message({"status": "ok", "run_id": "r1", "matched_events": 5, "picks": 1, "near_misses": 0},
                                picks, [])
    for expected in ("Arsenal v Chelsea", "Kickoff:", "UTC", "Lagos", "Outcome: away", "SportyBet price: 3.98",
                     "Sharp fair price: 3.98", "Edge: +3.17%", "Books used: pinnacle, matchbook"):
        assert expected in text, expected
    assert "Pro shadow (private test, not published)" in text


def test_no_pick_message_lists_top_three_near_misses():
    near = [_row(status="near_miss", home=f"Team{i}", edge=0.02 - i * 0.001, second_book=None, reason="thin")
            for i in range(4)]
    text = notify.build_message({"status": "ok", "run_id": "r2", "matched_events": 9, "picks": 0, "near_misses": 4},
                                [], near[:3])
    assert "No Pro pick today" in text
    assert text.count("- Team") == 3
    assert "Team3" not in text


def test_no_result_says_so_without_picks():
    assert "produced no result" in notify.build_message(None, [], [])


def test_send_goes_to_the_admin_chat_only_and_checks_status():
    calls = []

    def post(url, data, timeout):
        calls.append((url, data))
        return SimpleNamespace(status_code=200)

    sent = notify.send_to_admin(TOKEN, "8606823551", "hello", post)
    assert sent == 1
    url, data = calls[0]
    assert data["chat_id"] == "8606823551"
    assert url.startswith("https://api.telegram.org/bot")


def test_send_refuses_without_a_chat_or_token():
    with pytest.raises(ValueError):
        notify.send_to_admin(TOKEN, "", "x", lambda *a, **k: None)
    with pytest.raises(ValueError):
        notify.send_to_admin("", "1", "x", lambda *a, **k: None)


def test_failed_send_raises_with_status_only():
    with pytest.raises(RuntimeError) as err:
        notify.send_to_admin(TOKEN, "1", "x", lambda *a, **k: SimpleNamespace(status_code=403))
    assert "403" in str(err.value) and TOKEN not in str(err.value)


def test_long_messages_are_split_and_none_are_lost():
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
    chunks = notify.split_for_telegram(text, limit=500)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)
    assert "\n".join(chunks).split("\n") == text.split("\n")


def test_load_run_is_read_only_and_returns_picks_and_top_near_misses(tmp_path):
    path = str(tmp_path / "s.db")
    store = ShadowStore(path)
    rows = [_row(), _row(match_key="x|y|2", home="Lyon", away="Nice", status="near_miss", edge=0.01),
            _row(match_key="z|w|3", home="Genoa", away="Venezia", status="near_miss", edge=0.03)]
    store.insert_rows("r9", NOW, rows)
    store.close()
    picks, near = notify.load_run(path, "r9")
    assert [p["home"] for p in picks] == ["Arsenal"]
    assert [n["home"] for n in near] == ["Genoa", "Lyon"]  # sorted by edge, best first
    con = notify._connect_ro(path)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM pro_value_rows")
    con.close()


def test_stage5_notify_step_is_private_optional_and_after_the_shadow_step():
    import pathlib
    wf = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "stage5-acceptance-production.yml"
    text = wf.read_text(encoding="utf-8")
    names = [line.split("- name:", 1)[1].strip() for line in text.splitlines() if "- name:" in line]
    shadow = names.index("Pro value shadow (live only; shadow results only, publishes nothing)")
    notify_step = names.index("Send shadow Pro picks to the admin chat (private; continue-on-error)")
    assert notify_step == shadow + 1
    block = text.split("- name: Send shadow Pro picks to the admin chat", 1)[1].split("      - name:", 1)[0]
    assert "continue-on-error: true" in block
    assert "pro_value_notify.py" in block
    assert "wrangler" not in block and "d1 " not in block  # no D1 writes, no Worker changes
    assert "TELEGRAM_CHAT_ID" not in block  # never the channel
    assert "secrets.PRODUCTION_TELEGRAM_BOT_TOKEN" in block
