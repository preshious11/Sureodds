"""Scheduled closing capture: only picks 30-60 minutes from kickoff, and the daily cap survives across runs."""
from predbot.pro_value import closing
from predbot.pro_value.credits import CreditGuard
from predbot.pro_value.store import ShadowStore

NOW = 1_800_000_000


def _pick(kickoff_in_min):
    return {"league": "soccer_epl", "kickoff_ts": NOW + kickoff_in_min * 60}


def test_only_picks_30_to_60_minutes_out_are_due():
    rows = [_pick(20), _pick(29), _pick(30), _pick(45), _pick(60), _pick(61)]
    groups = closing.due_groups(rows, NOW, closing.CAPTURE_HI_MIN, closing.CAPTURE_LO_MIN)
    due = sorted(r["kickoff_ts"] for picks in groups.values() for r in picks)
    assert due == [NOW + 45 * 60, NOW + 60 * 60]


def test_default_bounds_keep_the_old_behaviour():
    rows = [_pick(5), _pick(20)]
    groups = closing.due_groups(rows, NOW, 20)
    assert sum(len(v) for v in groups.values()) == 2  # 0 < until <= 20 min, unchanged


def test_daily_spend_is_shared_across_runs(tmp_path):
    path = str(tmp_path / "shadow.db")
    first = ShadowStore(path)
    first.add_spent("2026-10-25", 3)
    first.close()
    second = ShadowStore(path)  # a later scheduled run opens the same DB
    spent = second.spent_on("2026-10-25")
    second.close()
    assert spent == 3
    guard = CreditGuard(reserve=80, cap=5, remaining=300, spent_today=spent)
    assert guard.can_spend(2) is True
    assert guard.can_spend(3) is False  # the 5/day cap holds across runs


def test_spend_is_per_utc_day(tmp_path):
    store = ShadowStore(str(tmp_path / "shadow.db"))
    store.add_spent("2026-10-25", 5)
    assert store.spent_on("2026-10-26") == 0
    store.close()
