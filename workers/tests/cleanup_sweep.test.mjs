// Real SQLite, real migrations — cleanupOldRows (CRON_WATCH daily
// housekeeping, bug 13) is pure DELETE statements, easiest to prove
// correct against the actual schema rather than a hand-rolled mock.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { cleanupOldRows } from "../src/cron.ts";

function stubDb(sql) {
  return {
    prepare(query) {
      return {
        bind(...args) {
          return {
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

function world() {
  const sql = new DatabaseSync(":memory:");
  for (const file of ["0001_foundation.sql", "0006_custom_sessions.sql", "0007_custom_picks_conversation.sql", "0009_match_events.sql"]) {
    sql.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  return { sql, env: { DB: stubDb(sql) } };
}

const iso = (ms) => new Date(ms).toISOString();
const NOW = Date.UTC(2026, 9, 17, 12, 0);
const DAY = 24 * 3_600_000;

describe("cleanupOldRows (CRON_WATCH daily housekeeping)", () => {
  it("deletes 'done' telegram_updates older than 7 days, keeps recent ones and non-done rows of any age", async () => {
    const { sql, env } = world();
    sql.prepare("INSERT INTO telegram_updates (update_id, status, updated_at) VALUES (?,?,?)").run(1, "done", iso(NOW - 8 * DAY));
    sql.prepare("INSERT INTO telegram_updates (update_id, status, updated_at) VALUES (?,?,?)").run(2, "done", iso(NOW - 6 * DAY));
    sql.prepare("INSERT INTO telegram_updates (update_id, status, updated_at) VALUES (?,?,?)").run(3, "failed", iso(NOW - 30 * DAY));
    await cleanupOldRows(env, NOW);
    const ids = sql.prepare("SELECT update_id FROM telegram_updates ORDER BY update_id").all().map((r) => r.update_id);
    assert.deepEqual(ids, [2, 3], "old done row deleted; recent done row and old failed row both kept");
  });

  it("deletes custom_suggestions past their own expires_at, keeps unexpired ones", async () => {
    const { sql, env } = world();
    sql.prepare(
      "INSERT INTO custom_suggestions (token, user_id, chat_id, label, request_json, ticket_json, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
    ).run("expired", "u1", "c1", "l", "{}", "{}", iso(NOW - DAY), iso(NOW - 1000));
    sql.prepare(
      "INSERT INTO custom_suggestions (token, user_id, chat_id, label, request_json, ticket_json, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
    ).run("live", "u1", "c1", "l", "{}", "{}", iso(NOW - DAY), iso(NOW + 100_000));
    await cleanupOldRows(env, NOW);
    const tokens = sql.prepare("SELECT token FROM custom_suggestions ORDER BY token").all().map((r) => r.token);
    assert.deepEqual(tokens, ["live"]);
  });

  it("deletes custom_sessions untouched for 2+ days, keeps recently-touched ones (active or not)", async () => {
    const { sql, env } = world();
    sql.prepare("INSERT INTO custom_sessions (user_id, chat_id, active, updated_at) VALUES (?,?,?,?)").run("u1", "c1", 1, iso(NOW - 3 * DAY));
    sql.prepare("INSERT INTO custom_sessions (user_id, chat_id, active, updated_at) VALUES (?,?,?,?)").run("u2", "c2", 0, iso(NOW - 3 * DAY));
    sql.prepare("INSERT INTO custom_sessions (user_id, chat_id, active, updated_at) VALUES (?,?,?,?)").run("u3", "c3", 1, iso(NOW - 1 * DAY));
    await cleanupOldRows(env, NOW);
    const remaining = sql.prepare("SELECT user_id FROM custom_sessions ORDER BY user_id").all().map((r) => r.user_id);
    assert.deepEqual(remaining, ["u3"], "both stale sessions (active or not) are gone; the recently-touched one survives");
  });

  it("deletes match_events older than 30 days, keeps recent ones", async () => {
    const { sql, env } = world();
    sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run("2026-09-01", "old-fixture", "start", "t", iso(NOW - 31 * DAY));
    sql.prepare("INSERT INTO match_events VALUES (?,?,?,?,?)").run("2026-10-10", "recent-fixture", "start", "t", iso(NOW - 7 * DAY));
    await cleanupOldRows(env, NOW);
    const keys = sql.prepare("SELECT fixture_key FROM match_events ORDER BY fixture_key").all().map((r) => r.fixture_key);
    assert.deepEqual(keys, ["recent-fixture"]);
  });

  it("is exactly 4 DELETE statements (one per table), comfortably inside the query budget", async () => {
    const { sql, env } = world();
    let count = 0;
    const counting = {
      prepare(query) {
        count += 1;
        return stubDb(sql).prepare(query);
      },
    };
    await cleanupOldRows({ DB: counting }, NOW);
    assert.equal(count, 4);
  });
});
