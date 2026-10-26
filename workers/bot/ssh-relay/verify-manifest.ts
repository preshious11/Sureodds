// Verifies a batch directory's plan.json checksums before apply-plan.ts is
// ever allowed to run - a transfer that was truncated or corrupted must
// never reach the database. Separate from apply-plan.ts so each has one
// job and both are independently testable.
//
// Usage: node verify-manifest.ts <batch-dir>
// plan.json shape: { "plan_id": "...", "files": [...], "checksums": { "<file>": "<sha256-hex>" } }
import { readFileSync, existsSync } from "node:fs";
import { createHash } from "node:crypto";
import { join } from "node:path";

export interface PlanManifest {
  plan_id: string;
  files: string[];
  checksums: Record<string, string>;
}

export function sha256OfFile(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

/** Throws a descriptive Error on the first problem found; returns the parsed manifest if everything checks out. */
export function verifyManifest(batchDir: string): PlanManifest {
  const manifestPath = join(batchDir, "plan.json");
  if (!existsSync(manifestPath)) throw new Error(`missing manifest: ${manifestPath}`);

  let manifest: PlanManifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (err) {
    throw new Error(`manifest is not valid JSON: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!manifest.plan_id || !Array.isArray(manifest.files) || manifest.files.length === 0) {
    throw new Error("manifest must have a non-empty plan_id and a non-empty files array");
  }
  if (!manifest.checksums || typeof manifest.checksums !== "object") {
    throw new Error("manifest must have a checksums map");
  }

  for (const file of manifest.files) {
    const expected = manifest.checksums[file];
    if (!expected) throw new Error(`manifest lists ${file} in files[] but has no checksum for it`);
    const path = join(batchDir, file);
    if (!existsSync(path)) throw new Error(`manifest lists ${file} but it is missing from the batch directory`);
    const actual = sha256OfFile(path);
    if (actual !== expected) {
      throw new Error(`checksum mismatch for ${file}: expected ${expected}, got ${actual}`);
    }
  }
  return manifest;
}

const isMain = process.argv[1]?.endsWith("verify-manifest.ts");
if (isMain) {
  const batchDir = process.argv[2];
  if (!batchDir) {
    console.error("usage: verify-manifest.ts <batch-dir>");
    process.exit(1);
  }
  try {
    const manifest = verifyManifest(batchDir);
    console.log(JSON.stringify({ event: "manifest_verified", plan_id: manifest.plan_id, files: manifest.files.length }));
    process.exit(0);
  } catch (err) {
    console.error(JSON.stringify({ event: "manifest_verification_failed", error: err instanceof Error ? err.message : String(err) }));
    process.exit(1);
  }
}
