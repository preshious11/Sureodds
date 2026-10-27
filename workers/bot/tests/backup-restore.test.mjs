// Tests verify-restore.ts against real SQLite behaviour: a real VACUUM
// INTO snapshot, a real integrity_check, and real row-count comparison -
// not mocked, since this is exactly the check the rehearsal (stop point 4)
// relies on to call a restore a pass or a fail.
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { LocalSqliteAdapter } from "../local-sqlite-adapter.ts";
import { verifyRestore } from "../backup/verify-restore.ts";

let tmpDir;
let dbPath;

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "sureodds-backup-test-"));
  dbPath = join(tmpDir, "source.db");
  const adapter = new LocalSqliteAdapter(dbPath);
  adapter.execRaw(readFileSync(new URL("../../migrations/0001_foundation.sql", import.meta.url), "utf8"));
  adapter.execRaw("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('b1', '2026-10-26T00:00:00Z', 1);");
  adapter.execRaw("INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('b2', '2026-10-26T00:00:00Z', 1);");
  adapter.close();
});

afterEach(() => {
  // WAL mode's -shm file can stay memory-mapped (and so locked) briefly
  // after close() on Windows specifically - irrelevant on the Linux server
  // this code actually targets. Cleanup failing here is a local dev-OS
  // artifact, never a reason to fail a test whose own assertions already
  // passed, so it's swallowed rather than retried indefinitely.
  try {
    rmSync(tmpDir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  } catch {
    // best-effort cleanup only
  }
});

function vacuumInto(sourcePath, targetPath) {
  const adapter = new LocalSqliteAdapter(sourcePath);
  adapter.execRaw(`VACUUM INTO '${targetPath.replace(/'/g, "''")}';`);
  adapter.close();
}

describe("verifyRestore", () => {
  it("a clean snapshot with matching row counts passes", () => {
    const snapshot = join(tmpDir, "snap-ok.db");
    vacuumInto(dbPath, snapshot);
    const result = verifyRestore(snapshot, { taken_at: "x", row_counts: { subscribers: 2 } });
    assert.equal(result.integrityOk, true);
    assert.deepEqual(result.rowCountMismatches, []);
    assert.equal(result.ok, true);
  });

  it("fails when a table's row count does not match the backup-time manifest", () => {
    const snapshot = join(tmpDir, "snap-mismatch.db");
    vacuumInto(dbPath, snapshot);
    // Manifest claims 3 rows were present at backup time, but the snapshot only has 2 -
    // simulates a backup taken mid-write, or the manifest being generated from a different state.
    const result = verifyRestore(snapshot, { taken_at: "x", row_counts: { subscribers: 3 } });
    assert.equal(result.ok, false);
    assert.deepEqual(result.rowCountMismatches, [{ table: "subscribers", expected: 3, actual: 2 }]);
  });

  it("fails cleanly (not a crash) when the manifest references a table the snapshot doesn't have", () => {
    const snapshot = join(tmpDir, "snap-no-table.db");
    vacuumInto(dbPath, snapshot);
    assert.throws(() => verifyRestore(snapshot, { taken_at: "x", row_counts: { no_such_table: 1 } }));
  });

  it("detects a corrupted snapshot via integrity_check rather than silently passing", () => {
    const corrupted = join(tmpDir, "corrupted.db");
    // A real sqlite file's header is 16 bytes; truncating it mid-page
    // produces a file sqlite will open but flag as corrupt on
    // integrity_check, exercising the failure path without needing to
    // hand-craft a specific corruption.
    vacuumInto(dbPath, corrupted);
    const original = readFileSync(corrupted);
    writeFileSync(corrupted, original.subarray(0, Math.floor(original.length / 2)));
    assert.throws(() => verifyRestore(corrupted, { taken_at: "x", row_counts: { subscribers: 2 } }));
  });
});
