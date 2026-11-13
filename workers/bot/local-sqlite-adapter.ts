// Local SQLite adapter: implements the same prepare().bind().first()/.all()/
// .run() surface as the Cloudflare D1 binding (and the old D1RestAdapter on
// feature/stage1-telegram-polling, which this supersedes) - everything
// above this file (workers/src/*.ts, unmodified) is unaware of the swap.
//
// Only sqlite-driver.ts names a concrete SQLite library - swapping it is a
// one-file change there, not here.
import { openSqlite, type SqliteConnection } from "./sqlite-driver.ts";

interface D1Meta {
  changes?: number;
  last_row_id?: number;
  [key: string]: unknown;
}

interface D1RunResult {
  meta: D1Meta;
}

interface D1AllResult<T> {
  results: T[];
}

interface D1BoundStatement<T = unknown> {
  first<U = T>(): Promise<U | null>;
  all<U = T>(): Promise<D1AllResult<U>>;
  run(): Promise<D1RunResult>;
}

interface D1PreparedStatement<T = unknown> extends D1BoundStatement<T> {
  bind(...params: unknown[]): D1BoundStatement<T>;
}

export interface D1Database {
  prepare<T = unknown>(sql: string): D1PreparedStatement<T>;
}

export interface BatchStatement {
  sql: string;
  params?: unknown[];
}

const PLAN_LOG_TABLE_SQL = `
CREATE TABLE IF NOT EXISTS sync_applied_plans (
  plan_id TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
`;

export class LocalSqliteAdapter implements D1Database {
  private readonly conn: SqliteConnection;

  constructor(path: string) {
    this.conn = openSqlite(path);
    // Needed by applyPlan() below - created here so a fresh database (e.g.
    // a brand-new rehearsal copy) always has it, without a separate
    // migration step.
    this.conn.exec(PLAN_LOG_TABLE_SQL);
  }

  prepare<T = unknown>(sql: string): D1PreparedStatement<T> {
    const conn = this.conn;

    const boundStatement = (params: unknown[]): D1BoundStatement<T> => ({
      async first<U = T>(): Promise<U | null> {
        const row = conn.prepareGet(sql, params);
        return row === undefined || row === null ? null : (normalizeRow(row) as U);
      },
      async all<U = T>(): Promise<D1AllResult<U>> {
        const rows = conn.prepareAll(sql, params);
        return { results: rows.map((r) => normalizeRow(r)) as U[] };
      },
      async run(): Promise<D1RunResult> {
        const info = conn.prepareRun(sql, params);
        return { meta: { changes: info.changes, last_row_id: Number(info.lastInsertRowid) } };
      },
    });

    return {
      bind(...params: unknown[]): D1BoundStatement<T> {
        return boundStatement(params);
      },
      ...boundStatement([]),
    };
  }

  /**
   * Atomic, like the real D1 binding's batch(): every statement commits
   * together or none do. Not called by anything in workers/src/ today (no
   * caller uses it - confirmed by audit), but this is now the permanent
   * adapter, not a bot-only shim, so it gets real transactional semantics
   * rather than being a stub that would silently do the wrong thing for a
   * future caller.
   */
  async batch(statements: BatchStatement[]): Promise<D1RunResult[]> {
    const conn = this.conn;
    conn.exec("BEGIN IMMEDIATE;");
    try {
      const results: D1RunResult[] = [];
      for (const { sql, params = [] } of statements) {
        const info = conn.prepareRun(sql, params);
        results.push({ meta: { changes: info.changes, last_row_id: Number(info.lastInsertRowid) } });
      }
      conn.exec("COMMIT;");
      return results;
    } catch (err) {
      conn.exec("ROLLBACK;");
      throw err;
    }
  }

  /**
   * Idempotent delivery for the GitHub Actions apply path (see
   * docs/STAGE1_SERVER_MIGRATION_PLAN.md section 6 and
   * ssh-relay/apply-plan.ts): runs every statement in `sqlStatements` as
   * ONE transaction, then records `planId` in the same transaction. A
   * repeated call with a `planId` already recorded is a no-op - it does
   * NOT re-run the statements, so a retried delivery after a network
   * interruption (the statements already committed, but the caller never
   * saw the success response) can never apply the same plan twice.
   *
   * `expectEpoch`, when given, is the same staleness guard
   * workers/scripts/d1_sync.py's `apply`/`push` commands already use
   * against D1 (see docs/STAGE1_SERVER_MIGRATION_PLAN.md's epoch
   * explanation) - but bundled into the SAME transaction as the data
   * statements and the epoch bump, which D1's REST API cannot do (its own
   * header comment calls this an "atomic-fencing limitation"). Checked
   * TWICE: once before BEGIN IMMEDIATE (fails fast on an already-obviously-
   * stale base without ever taking the write lock), and again inside the
   * transaction after BEGIN IMMEDIATE has acquired it (closes the race
   * where another writer's commit landed between the two checks - mirrors
   * d1_sync.py cmd_apply's own "before AND during" check). A mismatch
   * either time returns "stale_epoch" with NOTHING written - not even the
   * plan id - so a legitimate retry with a fresh base can still apply
   * cleanly. No caller (workers/src/*.ts, the bot, the 4 crons, any
   * cache-gating logic) ever reads sync_state.epoch - confirmed by audit -
   * so bundling the bump here cannot change what any reader already does.
   */
  /**
   * `paramStatements`, when given, are the SQLite-side equivalent of
   * d1_sync.py's `rest/*.jsonl` entries (oversized rows that don't fit D1's
   * 100KB SQL-text cap, applied individually via REST params instead).
   * SQLite has no such cap, but the plan is still generated with that
   * split baked in (cmd_plan decides it before the target is known), so
   * the relay accepts both shapes and applies them in the SAME
   * transaction as everything else - plain SQL text via exec(), and these
   * via the parameterized prepareRun() path (never string-interpolated
   * into SQL text, exactly like every other parameterized call in this
   * file).
   */
  applyPlan(
    planId: string,
    sqlStatements: string[],
    expectEpoch?: number,
    paramStatements: BatchStatement[] = [],
  ): "applied" | "already_applied" | "stale_epoch" {
    const conn = this.conn;
    const existing = conn.prepareGet("SELECT plan_id FROM sync_applied_plans WHERE plan_id = ?", [planId]);
    if (existing) return "already_applied";

    if (expectEpoch !== undefined && this.currentEpoch() !== expectEpoch) {
      return "stale_epoch";
    }

    conn.exec("BEGIN IMMEDIATE;");
    try {
      // Re-check plan_id too, not just the epoch: two concurrent deliveries
      // of the SAME plan can both pass the pre-lock `existing` check above
      // before either commits. Without this second check, the loser would
      // reach the INSERT below and throw a raw UNIQUE constraint violation
      // instead of returning "already_applied" - exactly the race this
      // function's whole design is meant to make impossible.
      const existingInTxn = conn.prepareGet("SELECT plan_id FROM sync_applied_plans WHERE plan_id = ?", [planId]);
      if (existingInTxn) {
        conn.exec("ROLLBACK;");
        return "already_applied";
      }
      if (expectEpoch !== undefined && this.currentEpoch() !== expectEpoch) {
        conn.exec("ROLLBACK;");
        return "stale_epoch";
      }
      for (const sql of sqlStatements) {
        conn.exec(sql);
      }
      for (const { sql, params = [] } of paramStatements) {
        conn.prepareRun(sql, params);
      }
      if (expectEpoch !== undefined) {
        conn.prepareRun(
          "UPDATE sync_state SET epoch = epoch + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = 1",
          [],
        );
      }
      conn.prepareRun("INSERT INTO sync_applied_plans (plan_id) VALUES (?)", [planId]);
      conn.exec("COMMIT;");
      return "applied";
    } catch (err) {
      conn.exec("ROLLBACK;");
      throw err;
    }
  }

  /** Current sync_state.epoch, or 0 if the row doesn't exist yet (fresh database, no migrations applied). */
  currentEpoch(): number {
    const row = this.conn.prepareGet("SELECT epoch FROM sync_state WHERE id = 1", []) as { epoch: number } | null;
    return row?.epoch ?? 0;
  }

  /** Raw passthrough for migrations and tests only - not part of the D1-compatible surface workers/src/ uses. */
  execRaw(sql: string): void {
    this.conn.exec(sql);
  }

  /** Raw passthrough for tests only - see execRaw(). */
  queryRaw(sql: string, params: unknown[] = []): unknown[] {
    return this.conn.prepareAll(sql, params).map(normalizeRow);
  }

  close(): void {
    this.conn.close();
  }
}

/** node:sqlite's .get()/.all() return null-prototype objects - normalize to plain objects so callers' assert.deepEqual et al. behave as expected. */
function normalizeRow(row: unknown): unknown {
  return row === null || typeof row !== "object" ? row : { ...(row as Record<string, unknown>) };
}
