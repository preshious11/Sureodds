// The ONLY file that names a specific SQLite library. local-sqlite-adapter.ts
// (and everything above it) talks to the minimal SqliteConnection interface
// below, never to node:sqlite or better-sqlite3 directly - swapping backends
// is a one-file change here, nowhere else.
//
// Default: node:sqlite (DatabaseSync), built into Node since 22.5 and, as of
// Node 24.15, Stability 1.2 - Release Candidate (no --experimental-sqlite
// flag needed; confirmed against https://nodejs.org/docs/latest-v24.x/api/sqlite.html).
// Not yet "2 - Stable", but:
//   - it's what workers/tests/*.test.mjs already uses (inline, as its own
//     env.DB mock) for all 572 of the Worker's existing tests, on the exact
//     Node version (24) this server runs - not a new, unproven dependency
//   - it has NO native binary: nothing to compile, no prebuilt binary to
//     match against this Node build's ABI, nothing to rebreak on a Node
//     point-release. On a 1 GB box with no build toolchain installed by
//     default, that is worth more than the one stability tier better-sqlite3
//     would add.
// better-sqlite3 is the fallback if node:sqlite's RC status ever regresses
// or a bug surfaces in practice - mature, synchronous, battle-tested, but a
// native module: it needs prebuilt binaries for this exact Node ABI/arch
// (Node 24 on x86_64 Linux - should have one, but that's one more thing that
// can silently stop matching after an unrelated Node upgrade) and a working
// node-gyp toolchain as a fallback when no prebuilt binary matches.
import { DatabaseSync } from "node:sqlite";

export interface SqliteRunResult {
  changes: number;
  lastInsertRowid: number | bigint;
}

/** The minimal surface local-sqlite-adapter.ts needs from any SQLite library. */
export interface SqliteConnection {
  exec(sql: string): void;
  prepareRun(sql: string, params: unknown[]): SqliteRunResult;
  prepareGet(sql: string, params: unknown[]): unknown;
  prepareAll(sql: string, params: unknown[]): unknown[];
  close(): void;
}

class NodeSqliteConnection implements SqliteConnection {
  private readonly db: DatabaseSync;

  constructor(path: string) {
    this.db = new DatabaseSync(path);
    // WAL + a busy timeout are the cross-process concurrency story (see
    // docs/STAGE1_SERVER_MIGRATION_PLAN.md section 5) - readers don't block
    // on a writer, and a second writer waits up to the timeout instead of
    // failing immediately. Set here, once, per connection.
    this.db.exec("PRAGMA journal_mode = WAL;");
    this.db.exec("PRAGMA busy_timeout = 5000;");
    this.db.exec("PRAGMA foreign_keys = ON;");
  }

  exec(sql: string): void {
    this.db.exec(sql);
  }

  prepareRun(sql: string, params: unknown[]): SqliteRunResult {
    const info = this.db.prepare(sql).run(...params);
    return { changes: Number(info.changes), lastInsertRowid: info.lastInsertRowid };
  }

  prepareGet(sql: string, params: unknown[]): unknown {
    return this.db.prepare(sql).get(...params) ?? null;
  }

  prepareAll(sql: string, params: unknown[]): unknown[] {
    return this.db.prepare(sql).all(...params);
  }

  close(): void {
    this.db.close();
  }
}

export function openSqlite(path: string): SqliteConnection {
  return new NodeSqliteConnection(path);
}
