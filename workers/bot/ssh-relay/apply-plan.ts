// CLI wrapper around LocalSqliteAdapter.applyPlan(), invoked by
// receive-batch.sh AFTER verify-manifest.ts has already confirmed the
// batch's checksums match (this script trusts the manifest's files[]
// list and does not re-verify - that is deliberately verify-manifest.ts's
// one job, not duplicated here). Reads plan.json + the listed .sql files,
// concatenates them in order, and applies them as ONE transaction under
// the plan's id - a repeated delivery of the same plan id is a no-op (see
// local-sqlite-adapter.test.mjs's applyPlan tests for the guarantee this
// relies on).
//
// Usage: node apply-plan.ts <sqlite-db-path> <batch-dir>
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { LocalSqliteAdapter } from "../local-sqlite-adapter.ts";
import { verifyManifest } from "./verify-manifest.ts";

function fail(message: string): never {
  console.error(JSON.stringify({ event: "apply_plan_failed", error: message }));
  process.exit(1);
}

const [dbPath, batchDir] = process.argv.slice(2);
if (!dbPath || !batchDir) {
  fail("usage: apply-plan.ts <sqlite-db-path> <batch-dir>");
}

let manifest;
try {
  // Re-reads and re-checks the manifest rather than trusting an
  // already-verified-elsewhere assumption - cheap, and means apply-plan.ts
  // is still safe to run standalone (as the smoke test / rehearsal does),
  // not only as receive-batch.sh's second step.
  manifest = verifyManifest(batchDir);
} catch (err) {
  fail(err instanceof Error ? err.message : String(err));
}

const sqlStatements = manifest.files.map((file) => readFileSync(join(batchDir, file), "utf8"));

const adapter = new LocalSqliteAdapter(dbPath);
try {
  const outcome = adapter.applyPlan(manifest.plan_id, sqlStatements);
  console.log(JSON.stringify({ event: "apply_plan", plan_id: manifest.plan_id, outcome, files: manifest.files.length }));
  process.exit(0);
} catch (err) {
  fail(`apply failed: ${err instanceof Error ? err.message : String(err)}`);
} finally {
  adapter.close();
}
