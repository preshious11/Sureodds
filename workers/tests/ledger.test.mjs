import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { ticketId, recordIssuedTicket, settleIssuedLeg, legReturn } from "../src/ledger.ts";

// Real SQLite, real migration file — not a hand-rolled schema, so a column
// rename in 0014 breaks this test instead of a live production insert.
function realDb() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(readFileSync(new URL("../migrations/0014_issued_picks_ledger.sql", import.meta.url), "utf8"));
  return {
    _sql: sql,
    prepare(query) {
      return {
        bind(...args) {
          return {
            async first() {
              return sql.prepare(query).get(...args) ?? null;
            },
            async all() {
              return { results: sql.prepare(query).all(...args) };
            },
            async run() {
              const info = sql.prepare(query).run(...args);
              return { meta: { changes: info.changes } };
            },
          };
        },
      };
    },
  };
}

function candidate(overrides = {}) {
  return {
    fixtureId: "fx-1", key: "k", sport: "football", market: "result2", selection: "home",
    price: 1.8, probability: 0.75, startMs: Date.UTC(2026, 9, 22, 18, 0), home: "Arsenal", away: "Chelsea",
    outcome: "Arsenal to Win", competition: null, competitionLabel: "Premier League",
    agreement: 0.9, liquidity: 0.5, books: 4, referenceEv: 0.02, line: null, period: "full",
    stat: "points", entity: "all", fixtureIdentity: "fx-1-identity", homeForm: [], awayForm: [], h2h: [],
    ...overrides,
  };
}

describe("ledger: idempotent issuance", () => {
  it("records a ticket and its legs exactly once, even if called twice for the same (feature, user, chat, day)", async () => {
    const db = realDb();
    const ticket = { picks: [candidate(), candidate({ fixtureId: "fx-2", fixtureIdentity: "fx-2-identity", home: "Liverpool", away: "Everton" })], odds: 2.5 };
    const first = await recordIssuedTicket(db, "pro", "7", "7", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    assert.equal(first.inserted, true);
    const second = await recordIssuedTicket(db, "pro", "7", "7", "2026-10-22", "Africa/Lagos", "2026-10-22T09:05:00Z", ticket);
    assert.equal(second.inserted, false, "a second issuance for the same day/destination/feature must be a no-op");
    assert.equal(second.id, first.id, "the id is content-derived, not random — same inputs, same id");

    const tickets = db._sql.prepare("SELECT COUNT(*) AS n FROM issued_tickets").get();
    assert.equal(tickets.n, 1, "exactly one ticket row, never duplicated");
    const legs = db._sql.prepare("SELECT COUNT(*) AS n FROM issued_ticket_legs WHERE ticket_id = ?").get(first.id);
    assert.equal(legs.n, 2, "both legs recorded once, not duplicated on the second call");
  });

  it("different features/users/chats/days never collide — each gets its own row", async () => {
    const db = realDb();
    const ticket = { picks: [candidate()], odds: 1.8 };
    await recordIssuedTicket(db, "pro", "7", "7", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    await recordIssuedTicket(db, "custom", "7", "7", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    await recordIssuedTicket(db, "pro", "9", "9", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    await recordIssuedTicket(db, "pro", "7", "7", "2026-10-23", "Africa/Lagos", "2026-10-23T09:00:00Z", ticket);
    const n = db._sql.prepare("SELECT COUNT(*) AS n FROM issued_tickets").get().n;
    assert.equal(n, 4, "feature, user, chat, and day are each part of the identity");
  });

  it("the id is deterministic/content-derived, not a random uuid", () => {
    const picks = [candidate()];
    assert.equal(ticketId("pro", "7", "7", "2026-10-22", picks), ticketId("pro", "7", "7", "2026-10-22", picks));
    assert.notEqual(ticketId("pro", "7", "7", "2026-10-22", picks), ticketId("custom", "7", "7", "2026-10-22", picks));
  });

  it("two genuinely DIFFERENT Custom Picks tickets from the same user on the same day are both recorded — never collapsed into one", async () => {
    const db = realDb();
    const morningTicket = { picks: [candidate({ fixtureId: "fx-football", sport: "football" })], odds: 1.8 };
    const eveningTicket = { picks: [candidate({ fixtureId: "fx-basketball", sport: "basketball", home: "Lakers", away: "Celtics" })], odds: 1.6 };
    const first = await recordIssuedTicket(db, "custom", "9", "c9", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", morningTicket);
    const second = await recordIssuedTicket(db, "custom", "9", "c9", "2026-10-22", "Africa/Lagos", "2026-10-22T18:00:00Z", eveningTicket);
    assert.equal(first.inserted, true);
    assert.equal(second.inserted, true, "a genuinely different ticket must NOT be treated as a duplicate of the morning one");
    assert.notEqual(first.id, second.id);
    const n = db._sql.prepare("SELECT COUNT(*) AS n FROM issued_tickets WHERE user_id='9' AND chat_id='c9' AND prediction_date='2026-10-22'").get().n;
    assert.equal(n, 2, "both of the user's distinct same-day tickets are recorded");
  });

  it("a RETRY of the exact same ticket (identical legs) is still deduplicated, even though same-day tickets are no longer deduplicated by day alone", async () => {
    const db = realDb();
    const ticket = { picks: [candidate()], odds: 1.8 };
    const first = await recordIssuedTicket(db, "custom", "9", "c9", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    const retry = await recordIssuedTicket(db, "custom", "9", "c9", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:05Z", ticket);
    assert.equal(first.inserted, true);
    assert.equal(retry.inserted, false, "identical legs for the same user/day IS a retry, and must still be deduplicated");
  });
});

describe("ledger: idempotent, non-contradictory settlement", () => {
  it("settles a leg exactly once; a repeated settlement call for the same leg is a no-op and never overwrites the recorded outcome", async () => {
    const db = realDb();
    const ticket = { picks: [candidate({ price: 2.0 })], odds: 2.0 };
    const { id } = await recordIssuedTicket(db, "pro", "7", "7", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    const firstSettle = await settleIssuedLeg(db, id, 0, "won", 2.0);
    assert.equal(firstSettle, true);
    const secondSettle = await settleIssuedLeg(db, id, 0, "lost", 2.0);
    assert.equal(secondSettle, false, "a leg already settled must never be re-settled, even to a different outcome");
    const row = db._sql.prepare("SELECT settlement_status, payout_multiplier FROM issued_ticket_legs WHERE ticket_id = ? AND leg_index = 0").get(id);
    assert.equal(row.settlement_status, "won", "the ORIGINAL outcome must survive, not the repeated call's");
    assert.equal(row.payout_multiplier, 2.0);
  });

  it("the parent ticket flips to 'settled' only once every leg has a real outcome, not before", async () => {
    const db = realDb();
    const ticket = { picks: [candidate({ fixtureId: "fx-1" }), candidate({ fixtureId: "fx-2", fixtureIdentity: "fx-2-identity" })], odds: 3.24 };
    const { id } = await recordIssuedTicket(db, "pro", "7", "7", "2026-10-22", "Africa/Lagos", "2026-10-22T09:00:00Z", ticket);
    await settleIssuedLeg(db, id, 0, "won", 1.8);
    let status = db._sql.prepare("SELECT status FROM issued_tickets WHERE id = ?").get(id).status;
    assert.equal(status, "issued", "still has a pending leg — must not be marked settled yet");
    await settleIssuedLeg(db, id, 1, "lost", 1.8);
    status = db._sql.prepare("SELECT status FROM issued_tickets WHERE id = ?").get(id).status;
    assert.equal(status, "settled", "every leg now has a real outcome");
  });

  it("legReturn matches standard unit-stake definitions, including push/void/half outcomes — never a naive binary formula", () => {
    assert.equal(legReturn("won", 2.5), 2.5);
    assert.equal(legReturn("lost", 2.5), 0);
    assert.equal(legReturn("push", 2.5), 1);
    assert.equal(legReturn("void", 2.5), 1);
    assert.equal(legReturn("half_won", 3.0), 2.0); // (1 + 3) / 2
    assert.equal(legReturn("half_lost", 3.0), 0.5);
    assert.equal(legReturn("pending", 2.5), null);
  });
});
