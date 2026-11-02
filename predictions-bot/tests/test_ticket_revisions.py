from copy import deepcopy

import pytest

from predbot.storage.db import Database


DAY = "2026-11-01"


def ticket_row(price):
    return {
        "pick_date": DAY,
        "revision_id": 1,
        "tier": "top",
        "rank": 1,
        "provider": "provider",
        "provider_event_id": "event-1",
        "sport": "football",
        "league": "League",
        "home": "Home",
        "away": "Away",
        "start_utc": "2026-11-01T20:00:00Z",
        "market": "result3",
        "period": "full",
        "stat": "points",
        "entity": "all",
        "line": None,
        "selection": "home",
        "label": "Home to win",
        "price": price,
        "prob": 0.6,
        "ev": 0.1,
        "created_at": "2026-11-01T08:00:00Z",
        "fixture_key": "football:event-1",
        "canonical_event_id": "event-1",
    }


def store_ticket(db, revision_id, price, collection="top"):
    row = ticket_row(price)
    row["rank"] = 1
    prediction_id = db.store_ticket_prediction(row, revision_id=revision_id, collection=collection)
    db.add_membership(prediction_id, collection, 1)
    return prediction_id


def test_new_revision_preserves_old_contract_and_serves_active_revision():
    db = Database(":memory:")
    try:
        with db.transaction():
            first = db.create_ticket_revision(DAY, "morning-evaluation", "2026-11-01T08:00:00Z")
            old_id = store_ticket(db, first, 1.44)
        old_before = deepcopy(dict(db.ticket_revision_predictions(DAY, "top", first)[0]))

        with db.transaction():
            second = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
            new_id = store_ticket(db, second, 2.2)

        assert first == 1
        assert second == 2
        assert old_id != new_id
        assert db.active_ticket_revision(DAY) == second
        assert [row["id"] for row in db.collection_predictions("top", DAY)] == [new_id]
        assert db.ticket_revision_predictions(DAY, "top", first)[0]["price"] == 1.44
        assert db.ticket_revision_predictions(DAY, "top", second)[0]["price"] == 2.2
        assert dict(db.ticket_revision_predictions(DAY, "top", first)[0]) == old_before
    finally:
        db.close()


def test_revision_publication_receipts_are_idempotent_and_only_definite_failures_retry():
    db = Database(":memory:")
    try:
        with db.transaction():
            revision = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
            store_ticket(db, revision, 2.2)

        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        assert not db.claim_ticket_publication(DAY, "top", revision, "group")
        db.finish_ticket_publication(DAY, "top", revision, "group", state="failed", error="rejected")
        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        db.finish_ticket_publication(DAY, "top", revision, "group", state="sent", message_id="123")
        assert not db.claim_ticket_publication(DAY, "top", revision, "group")
        assert db.ticket_publication(DAY, "top", revision, "group")["message_id"] == "123"

        assert db.claim_ticket_publication(DAY, "top", revision, "owner")
        db.finish_ticket_publication(DAY, "top", revision, "owner", state="uncertain", error="timeout")
        assert not db.claim_ticket_publication(DAY, "top", revision, "owner")
    finally:
        db.close()


def test_ticket_only_alternates_stay_out_of_all_but_representatives_keep_membership():
    db = Database(":memory:")
    try:
        alternate = ticket_row(2.2)
        alternate.update(provider_event_id="alternate", canonical_event_id="alternate",
                         fixture_key="football:alternate")
        alternate_id = db.store_ticket_prediction(alternate, revision_id=1, collection="top")
        assert [row["id"] for row in db.collection_predictions("top", DAY)] == [alternate_id]
        assert db.collection_predictions("all", DAY) == []

        representative = ticket_row(1.8)
        representative.update(provider_event_id="representative", canonical_event_id="representative",
                              fixture_key="football:representative")
        representative_id = db.store_prediction(representative)
        assert [row["id"] for row in db.collection_predictions("all", DAY)] == [representative_id]

        db.store_ticket_prediction(representative, revision_id=1, collection="top")
        assert [row["id"] for row in db.collection_predictions("all", DAY)] == [representative_id]
        assert [row["id"] for row in db.collection_predictions("top", DAY)] == [
            alternate_id, representative_id
        ]
    finally:
        db.close()


@pytest.mark.parametrize("published_revision", ["active", "historical"])
def test_reset_is_blocked_by_successful_publication_of_any_revision(published_revision):
    db = Database(":memory:")
    try:
        with db.transaction():
            first = db.create_ticket_revision(DAY, "morning-evaluation", "2026-11-01T08:00:00Z")
            second = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
        revision = second if published_revision == "active" else first
        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        db.finish_ticket_publication(DAY, "top", revision, "group", state="sent", message_id="123")

        with pytest.raises(ValueError, match="published prediction day"):
            db.reset_prediction_day(DAY, "UTC", "2026-11-01T00:00:00Z")

        assert db.ticket_publication(DAY, "top", revision, "group")["message_id"] == "123"
        assert db.active_ticket_revision(DAY) == second
    finally:
        db.close()


def test_stale_sending_claim_becomes_uncertain_and_is_not_auto_retryable():
    db = Database(":memory:")
    try:
        with db.transaction():
            revision = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
            store_ticket(db, revision, 2.2)

        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        # Simulate a process that died between claim and finish: back-date the
        # claim past the staleness window without ever calling finish_ticket_publication.
        db.execute("UPDATE ticket_publications SET updated_at=? WHERE pick_date=? AND collection=? "
                   "AND revision_id=? AND destination=?",
                   ("2020-01-01T00:00:00Z", DAY, "top", revision, "group"))

        swept = db.sweep_stale_ticket_publications(timeout_seconds=300)
        assert swept == 1
        receipt = db.ticket_publication(DAY, "top", revision, "group")
        assert receipt["state"] == "uncertain"
        assert "stale claim" in receipt["error"]
        # Audit fields untouched by the sweep.
        assert receipt["revision_id"] == revision
        assert receipt["destination"] == "group"

        # An uncertain receipt must never be picked up as a retryable failure.
        assert not db.claim_ticket_publication(DAY, "top", revision, "group")

        # A fresh, non-stale 'sending' claim is left alone.
        with db.transaction():
            second = db.create_ticket_revision(DAY, "second-evaluation", "2026-11-01T11:00:00Z")
            store_ticket(db, second, 1.9)
        assert db.claim_ticket_publication(DAY, "top", second, "group")
        assert db.sweep_stale_ticket_publications(timeout_seconds=300) == 0
        assert db.ticket_publication(DAY, "top", second, "group")["state"] == "sending"
    finally:
        db.close()


def test_confirmed_failed_publication_remains_retryable_after_sweep():
    db = Database(":memory:")
    try:
        with db.transaction():
            revision = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
            store_ticket(db, revision, 2.2)

        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        db.finish_ticket_publication(DAY, "top", revision, "group", state="failed", error="send failed")

        # The sweep must not disturb a confirmed terminal state.
        assert db.sweep_stale_ticket_publications(timeout_seconds=0) == 0
        assert db.ticket_publication(DAY, "top", revision, "group")["state"] == "failed"
        assert db.claim_ticket_publication(DAY, "top", revision, "group")
    finally:
        db.close()


def test_crash_after_possible_telegram_send_does_not_auto_duplicate():
    """A process dying right after tg.send() (message possibly delivered) must
    never let the next run silently resend - only a human-reviewed path can."""
    db = Database(":memory:")
    try:
        with db.transaction():
            revision = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
            store_ticket(db, revision, 2.2)

        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        db.execute("UPDATE ticket_publications SET updated_at=? WHERE pick_date=? AND collection=? "
                   "AND revision_id=? AND destination=?",
                   ("2020-01-01T00:00:00Z", DAY, "top", revision, "group"))
        db.sweep_stale_ticket_publications(timeout_seconds=300)

        for _ in range(3):
            assert not db.claim_ticket_publication(DAY, "top", revision, "group")
        assert db.ticket_publication(DAY, "top", revision, "group")["state"] == "uncertain"
    finally:
        db.close()


@pytest.mark.parametrize("published_revision", ["active", "historical"])
def test_reset_is_blocked_by_uncertain_publication_of_any_revision(published_revision):
    db = Database(":memory:")
    try:
        with db.transaction():
            first = db.create_ticket_revision(DAY, "morning-evaluation", "2026-11-01T08:00:00Z")
            second = db.create_ticket_revision(DAY, "fresh-evaluation", "2026-11-01T10:00:00Z")
        revision = second if published_revision == "active" else first
        assert db.claim_ticket_publication(DAY, "top", revision, "group")
        db.finish_ticket_publication(DAY, "top", revision, "group", state="uncertain", error="timeout")

        with pytest.raises(ValueError, match="published prediction day"):
            db.reset_prediction_day(DAY, "UTC", "2026-11-01T00:00:00Z")

        assert db.ticket_publication(DAY, "top", revision, "group")["state"] == "uncertain"
        assert db.active_ticket_revision(DAY) == second
    finally:
        db.close()


def test_legacy_community_receipts_migrate_with_their_group_recipient(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy-publication.db")
    db = Database(path)
    db.mark_delivery("community_picks_top", DAY, "-1001234567890")
    db.close()

    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE ticket_publications DROP COLUMN recipient_id")

    db = Database(path)
    try:
        receipt = db.ticket_publication(DAY, "top", 1, "group")
        assert receipt["state"] == "sent"
        assert receipt["recipient_id"] == "-1001234567890"
    finally:
        db.close()
