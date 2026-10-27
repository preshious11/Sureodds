// Tests for the SSH relay's two pieces: verify-manifest.ts (checksum
// validation) and apply-plan.ts as an actual child process (so these tests
// exercise the real CLI entry points receive-batch.sh invokes, not just
// their exported internals) - including the two scenarios explicitly
// asked for: an interrupted delivery and a duplicate delivery.
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { verifyManifest, sha256OfFile } from "../ssh-relay/verify-manifest.ts";
import { LocalSqliteAdapter } from "../local-sqlite-adapter.ts";

let tmpDir;
let dbPath;
let batchDir;

function sha(content) {
  return createHash("sha256").update(content).digest("hex");
}

function writeBatch(dir, planId, fileContents, expectEpoch) {
  const checksums = {};
  for (const [name, content] of Object.entries(fileContents)) {
    writeFileSync(join(dir, name), content, "utf8");
    checksums[name] = sha(content);
  }
  const manifest = { plan_id: planId, files: Object.keys(fileContents), checksums };
  if (expectEpoch !== undefined) manifest.expect_epoch = expectEpoch;
  writeFileSync(join(dir, "plan.json"), JSON.stringify(manifest), "utf8");
}

function makeDatabase(path) {
  const adapter = new LocalSqliteAdapter(path);
  adapter.execRaw(readFileSync(new URL("../../migrations/0001_foundation.sql", import.meta.url), "utf8"));
  adapter.execRaw(readFileSync(new URL("../../migrations/0004_sync_state.sql", import.meta.url), "utf8"));
  adapter.close();
}

function runApplyPlan(args) {
  const scriptPath = fileURLToPath(new URL("../ssh-relay/apply-plan.ts", import.meta.url));
  return execFileSync(process.execPath, [scriptPath, ...args], { encoding: "utf8" });
}

/** Like runApplyPlan, but returns {status, stdout} instead of throwing on a non-zero exit (needed for stale_epoch's exit code 2). */
function runApplyPlanAllowingFailure(args) {
  const scriptPath = fileURLToPath(new URL("../ssh-relay/apply-plan.ts", import.meta.url));
  try {
    const stdout = execFileSync(process.execPath, [scriptPath, ...args], { encoding: "utf8" });
    return { status: 0, stdout };
  } catch (err) {
    return { status: err.status, stdout: err.stdout, stderr: err.stderr };
  }
}

beforeEach(() => {
  tmpDir = mkdtempSync(join(tmpdir(), "sureodds-relay-test-"));
  dbPath = join(tmpDir, "test.db");
  batchDir = join(tmpDir, "batch");
  mkdirSync(batchDir, { recursive: true });
  makeDatabase(dbPath);
});

afterEach(() => {
  rmSync(tmpDir, { recursive: true, force: true });
});

describe("verifyManifest", () => {
  it("accepts a manifest whose checksums match the actual files", () => {
    writeBatch(batchDir, "plan-ok", { "0001.sql": "SELECT 1;" });
    const manifest = verifyManifest(batchDir);
    assert.equal(manifest.plan_id, "plan-ok");
  });

  it("rejects a manifest with a checksum that doesn't match the file - simulates a truncated/corrupted transfer", () => {
    writeBatch(batchDir, "plan-bad", { "0001.sql": "SELECT 1;" });
    // Corrupt the file AFTER its checksum was recorded - as a truncated
    // transfer would leave it.
    writeFileSync(join(batchDir, "0001.sql"), "SELECT 1; -- truncated differently", "utf8");
    assert.throws(() => verifyManifest(batchDir), /checksum mismatch/);
  });

  it("rejects a manifest listing a file that is missing from the batch directory", () => {
    writeBatch(batchDir, "plan-missing", { "0001.sql": "SELECT 1;" });
    rmSync(join(batchDir, "0001.sql"));
    assert.throws(() => verifyManifest(batchDir), /missing from the batch directory/);
  });

  it("rejects a batch directory with no plan.json at all", () => {
    assert.throws(() => verifyManifest(batchDir), /missing manifest/);
  });

  it("sha256OfFile matches node:crypto's own hash of the same bytes", () => {
    writeFileSync(join(batchDir, "x.sql"), "hello", "utf8");
    assert.equal(sha256OfFile(join(batchDir, "x.sql")), sha("hello"));
  });

  it("rejects a manifest with no files, no statements, and no expect_epoch - genuinely nothing to apply", () => {
    writeBatch(batchDir, "plan-empty", {});
    assert.throws(() => verifyManifest(batchDir), /nothing to apply/);
  });

  it("accepts a manifest with no files/statements WHEN expect_epoch is set - an epoch-only plan (settlement found nothing new to grade), matching production D1's unconditional epoch bump", () => {
    writeBatch(batchDir, "plan-epoch-only", {}, 7);
    const manifest = verifyManifest(batchDir);
    assert.equal(manifest.plan_id, "plan-epoch-only");
    assert.equal(manifest.expect_epoch, 7);
  });
});

describe("apply-plan.ts as a child process: duplicate and interrupted delivery", () => {
  it("applies a fresh plan, then a second identical delivery is a no-op", () => {
    writeBatch(batchDir, "plan-dup", {
      "0001.sql": "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('relay1', '2026-10-26T00:00:00Z', 1);",
    });
    const first = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(first.outcome, "applied");

    const second = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(second.outcome, "already_applied");

    const adapter = new LocalSqliteAdapter(dbPath);
    const rows = adapter.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'relay1'");
    adapter.close();
    assert.equal(rows.length, 1, "duplicate delivery must not insert the row twice");
  });

  it("an interrupted delivery (corrupted file after the checksum was taken) is rejected before touching the database, and a clean retry of the same plan id then succeeds", () => {
    writeBatch(batchDir, "plan-interrupted", {
      "0001.sql": "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('relay2', '2026-10-26T00:00:00Z', 1);",
    });
    // Simulate the interruption: the file on disk no longer matches the
    // checksum recorded in plan.json (as a transfer cut short mid-write
    // would leave it).
    writeFileSync(join(batchDir, "0001.sql"), "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('corrupted'", "utf8");

    assert.throws(() => runApplyPlan([dbPath, batchDir]));
    const adapter1 = new LocalSqliteAdapter(dbPath);
    const duringFailure = adapter1.queryRaw("SELECT chat_id FROM subscribers");
    adapter1.close();
    assert.deepEqual(duringFailure, [], "a rejected manifest must not reach the database at all");

    // The sender re-transfers the batch correctly (same plan id, now-correct file).
    writeBatch(batchDir, "plan-interrupted", {
      "0001.sql": "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('relay2', '2026-10-26T00:00:00Z', 1);",
    });
    const retry = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(retry.outcome, "applied");
    const adapter2 = new LocalSqliteAdapter(dbPath);
    const after = adapter2.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'relay2'");
    adapter2.close();
    assert.equal(after.length, 1);
  });
});

describe("get-epoch.ts: read-only epoch query, read immediately before building a plan manifest", () => {
  it("prints the current epoch as JSON", () => {
    const scriptPath = fileURLToPath(new URL("../ssh-relay/get-epoch.ts", import.meta.url));
    const output = execFileSync(process.execPath, [scriptPath, dbPath], { encoding: "utf8" });
    const parsed = JSON.parse(output);
    const adapter = new LocalSqliteAdapter(dbPath);
    const expected = adapter.currentEpoch();
    adapter.close();
    assert.equal(parsed.epoch, expected);
  });

  it("reflects a bump made by a prior applyPlan call - not a stale read", () => {
    const scriptPath = fileURLToPath(new URL("../ssh-relay/get-epoch.ts", import.meta.url));
    const before = JSON.parse(execFileSync(process.execPath, [scriptPath, dbPath], { encoding: "utf8" })).epoch;

    const adapter = new LocalSqliteAdapter(dbPath);
    adapter.applyPlan("plan-for-epoch-bump-check", ["INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('x', '2026-10-26T00:00:00Z', 1)"], before);
    adapter.close();

    const after = JSON.parse(execFileSync(process.execPath, [scriptPath, dbPath], { encoding: "utf8" })).epoch;
    assert.equal(after, before + 1);
  });
});

describe("apply-plan.ts: manifest statements[] (the rest/*.jsonl equivalent)", () => {
  it("applies a manifest whose statements[] carries all the data (no files at all)", () => {
    writeFileSync(
      join(batchDir, "plan.json"),
      JSON.stringify({
        plan_id: "plan-stmt-only",
        files: [],
        checksums: {},
        statements: [
          {
            sql: "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES (?, ?, ?)",
            params: ["relay5", "2026-10-26T00:00:00Z", 1],
          },
        ],
      }),
      "utf8",
    );
    const result = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(result.outcome, "applied");
    assert.equal(result.statements, 1);

    const adapter = new LocalSqliteAdapter(dbPath);
    const rows = adapter.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'relay5'");
    adapter.close();
    assert.equal(rows.length, 1);
  });
});

describe("apply-plan.ts: epoch passthrough from the manifest", () => {
  it("a manifest with a correct expect_epoch applies and bumps epoch", () => {
    const adapter = new LocalSqliteAdapter(dbPath);
    const before = adapter.currentEpoch();
    adapter.close();

    writeBatch(
      batchDir,
      "plan-epoch-ok",
      { "0001.sql": "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('relay3', '2026-10-26T00:00:00Z', 1);" },
      before,
    );
    const result = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(result.outcome, "applied");

    const after = new LocalSqliteAdapter(dbPath);
    assert.equal(after.currentEpoch(), before + 1);
    after.close();
  });

  it("a manifest with a stale expect_epoch exits 2 and applies nothing - the workflow's signal to re-pull and retry", () => {
    const adapter = new LocalSqliteAdapter(dbPath);
    const before = adapter.currentEpoch();
    adapter.close();

    writeBatch(
      batchDir,
      "plan-epoch-stale",
      { "0001.sql": "INSERT INTO subscribers (chat_id, subscribed_at, active) VALUES ('relay4', '2026-10-26T00:00:00Z', 1);" },
      before + 999,
    );
    const { status, stdout } = runApplyPlanAllowingFailure([dbPath, batchDir]);
    assert.equal(status, 2);
    assert.equal(JSON.parse(stdout).outcome, "stale_epoch");

    const after = new LocalSqliteAdapter(dbPath);
    assert.equal(after.currentEpoch(), before, "a stale-epoch rejection must not bump epoch");
    const rows = after.queryRaw("SELECT chat_id FROM subscribers WHERE chat_id = 'relay4'");
    after.close();
    assert.deepEqual(rows, [], "a stale-epoch rejection must not apply the data either");
  });

  it("an epoch-only plan (no files, no statements) still applies and bumps epoch - matches production D1's unconditional epoch bump on a settlement run that found nothing new to grade", () => {
    const adapter = new LocalSqliteAdapter(dbPath);
    const before = adapter.currentEpoch();
    adapter.close();

    writeBatch(batchDir, "plan-epoch-only-ok", {}, before);
    const result = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(result.outcome, "applied");
    assert.equal(result.files, 0);
    assert.equal(result.statements, 0);

    const after = new LocalSqliteAdapter(dbPath);
    assert.equal(after.currentEpoch(), before + 1);
    after.close();
  });

  it("a repeated epoch-only plan (same plan id) is a no-op - does not bump epoch a second time", () => {
    const adapter = new LocalSqliteAdapter(dbPath);
    const before = adapter.currentEpoch();
    adapter.close();

    writeBatch(batchDir, "plan-epoch-only-repeat", {}, before);
    JSON.parse(runApplyPlan([dbPath, batchDir]));
    const second = JSON.parse(runApplyPlan([dbPath, batchDir]));
    assert.equal(second.outcome, "already_applied");

    const after = new LocalSqliteAdapter(dbPath);
    assert.equal(after.currentEpoch(), before + 1, "the second (deduped) delivery must not bump epoch again");
    after.close();
  });
});
