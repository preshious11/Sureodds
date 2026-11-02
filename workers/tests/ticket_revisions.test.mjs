import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { getActiveTicketRevision, getAllPicksForCollection, claimTicketPublication, sweepStaleTicketPublications } from "../src/db.ts";
import { postGroupPicks } from "../src/cron.ts";

const DAY = "2026-11-01";
const NOW = Date.UTC(2026, 10, 1, 10, 0); // 11:00 Lagos
const MIGRATIONS = [
  "0001_foundation.sql", "0003_engine_state.sql", "0004_sync_state.sql", "0005_ticket_candidates.sql",
  "0006_custom_sessions.sql", "0007_custom_picks_conversation.sql", "0008_pinned_menu.sql",
  "0009_match_events.sql", "0010_sync_lease.sql", "0011_pipeline_dispatch.sql", "0012_result_gifs.sql",
  "0013_pro_picks.sql", "0014_issued_picks_ledger.sql", "0015_selection_rejections.sql",
  "0016_closing_capture_columns.sql", "0017_oracle_recovery_guards.sql",
];

function world(withLegacy = true) {
  const sql = new DatabaseSync(":memory:");
  for (const migration of MIGRATIONS) {
    sql.exec(readFileSync(new URL(`../migrations/${migration}`, import.meta.url), "utf8"));
  }
  if (withLegacy) {
    sql.exec(`INSERT INTO picks(id,pick_date,tier,rank,provider,provider_event_id,sport,home,away,start_utc,
      market,period,stat,entity,selection,label,price,prob,ev,status,created_at,fixture_key,canonical_event_id,
      is_representative) VALUES(7,'${DAY}','all',1,'p','e','football','A','B',
      '${DAY}T20:00:00Z','result3','full','points','all','home','A to win',1.44,.7,0,'pending',
      '${DAY}T08:00:00Z','fixture-a','event-a',1);
      INSERT INTO prediction_memberships VALUES(7,'all',1),(7,'top',1);
      INSERT INTO posts(kind,post_date,message_id,created_at) VALUES
        ('top','${DAY}','old-message','${DAY}T08:30:00Z');`);
  }
  sql.exec(readFileSync(new URL("../migrations/0018_ticket_publication_revisions.sql", import.meta.url), "utf8"));
  sql.exec(readFileSync(new URL("../migrations/0019_ticket_publication_recipient.sql", import.meta.url), "utf8"));
  const DB = {
    prepare(query) {
      return {
        args: [],
        bind(...args) { this.args = args; return this; },
        async first() { return sql.prepare(query).get(...this.args) ?? null; },
        async all() { return { results: sql.prepare(query).all(...this.args) }; },
        async run() {
          const result = sql.prepare(query).run(...this.args);
          return { meta: { changes: result.changes } };
        },
      };
    },
  };
  sql.prepare("INSERT INTO prediction_cache(cache_key,local_date,timezone,status,payload_json,generated_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?)")
    .run("test", DAY, "Africa/Lagos", "COMPLETE", "{}", "now", "now", "later");
  return { sql, DB };
}

function addPick(sql, { id, revision, collection, price }) {
  sql.prepare(`INSERT INTO picks(id,pick_date,revision_id,tier,rank,provider,provider_event_id,sport,home,away,start_utc,
    market,period,stat,entity,selection,label,price,prob,ev,status,created_at,fixture_key,canonical_event_id,
    is_representative) VALUES(?,?,?,?,?,'p',?,'football','C','D',?,'result3','full','points','all','home',
    'C to win',?,.7,0,'pending',?,?,?,1)`)
    .run(id, DAY, revision, collection, 1, `event-${id}`, `${DAY}T20:00:00Z`, price, `${DAY}T09:00:00Z`,
      `fixture-${id}`, `event-${id}`);
  sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)").run(id, "all", 1);
  sql.prepare("INSERT INTO prediction_memberships VALUES(?,?,?)").run(id, collection, 1);
}

describe("revision-aware ticket lifecycle", () => {
  it("serves the active revision, keeps the legacy contract auditable, and posts once", async () => {
    const { sql, DB } = world();
    try {
      assert.equal(await getActiveTicketRevision(DB, DAY), 1);
      assert.equal(sql.prepare("SELECT price FROM picks WHERE id=7").get().price, 1.44);
      assert.equal(sql.prepare("SELECT message_id FROM posts WHERE kind='top'").get().message_id, "old-message");
      assert.equal(sql.prepare("SELECT state FROM ticket_publications WHERE revision_id=1 AND collection='top'").get().state, "sent");

      sql.prepare("INSERT INTO ticket_revisions VALUES(?,?,?,?)").run(DAY, 2, "fresh-top", `${DAY}T09:00:00Z`);
      sql.prepare("UPDATE active_ticket_revisions SET revision_id=2,updated_at=? WHERE pick_date=?")
        .run(`${DAY}T09:00:00Z`, DAY);
      addPick(sql, { id: 8, revision: 2, collection: "top", price: 2.2 });
      assert.equal((await getAllPicksForCollection(DB, DAY, "top"))[0].id, 8);
      assert.equal((await getAllPicksForCollection(DB, DAY, "top", 1))[0].id, 7);
      assert.equal((await getAllPicksForCollection(DB, DAY, "top", 1))[0].price, 1.44);

      const env = { DB, TIMEZONE: "Africa/Lagos", TELEGRAM_CHAT_ID: "-100", DELIVERY_TIME: "09:30" };
      let sends = 0;
      const sender = async () => ({ ok: true, messageId: ++sends });
      assert.equal(await postGroupPicks(env, "top", NOW, sender), "sent");
      assert.equal(await postGroupPicks(env, "top", NOW, sender), "skipped");
      assert.equal(sends, 1);
      const groupReceipt = sql.prepare("SELECT state,message_id,recipient_id FROM ticket_publications WHERE revision_id=2 AND collection='top' AND destination='group'")
        .get();
      assert.equal(groupReceipt.state, "sent");
      assert.equal(groupReceipt.recipient_id, "-100");
      assert.equal(sql.prepare("SELECT message_id FROM posts WHERE kind='top'").get().message_id, "old-message");
    } finally {
      sql.close();
    }
  });

  it("retries a definite failed send, but publishes nothing for an empty ticket", async () => {
    const { sql, DB } = world(false);
    try {
      sql.prepare("INSERT INTO ticket_revisions VALUES(?,?,?,?)").run(DAY, 1, "fresh-daily", `${DAY}T09:00:00Z`);
      sql.prepare("INSERT INTO active_ticket_revisions VALUES(?,?,?)").run(DAY, 1, `${DAY}T09:00:00Z`);
      addPick(sql, { id: 10, revision: 1, collection: "daily", price: 10.5 });
      const env = { DB, TIMEZONE: "Africa/Lagos", TELEGRAM_CHAT_ID: "-100", DELIVERY_TIME: "09:30" };
      let sends = 0;
      assert.equal(await postGroupPicks(env, "daily", NOW, async () => {
        sends += 1;
        return { ok: false };
      }), "failed");
      assert.equal(sql.prepare("SELECT state FROM ticket_publications WHERE collection='daily' AND destination='group'")
        .get().state, "failed");
      assert.equal(await postGroupPicks(env, "daily", NOW, async () => {
        sends += 1;
        return { ok: true, messageId: 99 };
      }), "sent");
      assert.equal(sends, 2);
      assert.equal(sql.prepare("SELECT state,message_id FROM ticket_publications WHERE collection='daily' AND destination='group'")
        .get().state, "sent");

      assert.equal(await postGroupPicks(env, "top", NOW, async () => {
        sends += 1;
        return { ok: true, messageId: 100 };
      }), "skipped");
      assert.equal(sends, 2);
      assert.equal(sql.prepare("SELECT COUNT(*) AS n FROM ticket_publications WHERE collection='top'").get().n, 0);
    } finally {
      sql.close();
    }
  });

  it("transitions a stale sending claim to uncertain and never auto-retries it", async () => {
    const { sql, DB } = world(false);
    try {
      sql.prepare("INSERT INTO ticket_revisions VALUES(?,?,?,?)").run(DAY, 1, "fresh-top", `${DAY}T09:00:00Z`);
      sql.prepare("INSERT INTO active_ticket_revisions VALUES(?,?,?)").run(DAY, 1, `${DAY}T09:00:00Z`);
      addPick(sql, { id: 20, revision: 1, collection: "top", price: 1.6 });

      assert.equal(await claimTicketPublication(DB, DAY, "top", 1, "group", "-100"), true);
      // Simulate the Worker crashing between claim and finishTicketPublication.
      sql.prepare("UPDATE ticket_publications SET updated_at=? WHERE pick_date=? AND collection=? AND revision_id=? AND destination=?")
        .run("2020-01-01T00:00:00Z", DAY, "top", 1, "group");

      const swept = await sweepStaleTicketPublications(DB, 300);
      assert.equal(swept, 1);
      const row = sql.prepare("SELECT state,recipient_id,error FROM ticket_publications WHERE pick_date=? AND collection='top' AND revision_id=1 AND destination='group'")
        .get(DAY);
      assert.equal(row.state, "uncertain");
      assert.equal(row.recipient_id, "-100");
      assert.ok(row.error.includes("stale claim"));

      // Never retryable automatically - claimTicketPublication only reclaims 'failed'.
      assert.equal(await claimTicketPublication(DB, DAY, "top", 1, "group", "-100"), false);

      // A crash-recovery sweep from a later postGroupPicks run must not resend.
      let sends = 0;
      const outcome = await postGroupPicks(
        { DB, TIMEZONE: "Africa/Lagos", TELEGRAM_CHAT_ID: "-100", DELIVERY_TIME: "09:30" },
        "top", NOW, async () => { sends += 1; return { ok: true, messageId: 1 }; },
      );
      assert.equal(outcome, "skipped");
      assert.equal(sends, 0);
      assert.equal(sql.prepare("SELECT state FROM ticket_publications WHERE pick_date=? AND collection='top' AND revision_id=1 AND destination='group'")
        .get(DAY).state, "uncertain");
    } finally {
      sql.close();
    }
  });

  it("leaves a confirmed failed claim retryable after a stale sweep", async () => {
    const { sql, DB } = world(false);
    try {
      sql.prepare("INSERT INTO ticket_revisions VALUES(?,?,?,?)").run(DAY, 1, "fresh-top", `${DAY}T09:00:00Z`);
      sql.prepare("INSERT INTO active_ticket_revisions VALUES(?,?,?)").run(DAY, 1, `${DAY}T09:00:00Z`);
      addPick(sql, { id: 21, revision: 1, collection: "top", price: 1.6 });

      assert.equal(await claimTicketPublication(DB, DAY, "top", 1, "group", "-100"), true);
      sql.prepare("UPDATE ticket_publications SET state='failed',updated_at=? WHERE pick_date=? AND collection=? AND revision_id=? AND destination=?")
        .run("2020-01-01T00:00:00Z", DAY, "top", 1, "group");

      assert.equal(await sweepStaleTicketPublications(DB, 300), 0);
      assert.equal(sql.prepare("SELECT state FROM ticket_publications WHERE pick_date=? AND collection='top' AND revision_id=1 AND destination='group'")
        .get(DAY).state, "failed");
      assert.equal(await claimTicketPublication(DB, DAY, "top", 1, "group", "-100"), true);
    } finally {
      sql.close();
    }
  });
});
