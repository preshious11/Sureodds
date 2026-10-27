// Verifies a backup snapshot is actually restorable: opens it, runs
// PRAGMA integrity_check, and compares its per-table row counts against
// the manifest taken at backup time (backup.sh). This is the "tested
// restore" half of the backup requirement - run against a downloaded
// snapshot during the rehearsal, not just trusted because the upload
// succeeded.
//
// Usage: node verify-restore.ts <snapshot-db-path> <manifest-json-path>
import { readFileSync } from "node:fs";
import { LocalSqliteAdapter } from "../local-sqlite-adapter.ts";

export interface BackupManifest {
  taken_at: string;
  row_counts: Record<string, number>;
}

export interface RestoreVerification {
  integrityOk: boolean;
  integrityMessage: string;
  rowCountMismatches: Array<{ table: string; expected: number; actual: number }>;
  ok: boolean;
}

export function verifyRestore(snapshotPath: string, manifest: BackupManifest): RestoreVerification {
  const adapter = new LocalSqliteAdapter(snapshotPath);
  try {
    const integrityRows = adapter.queryRaw("PRAGMA integrity_check;") as Array<{ integrity_check: string }>;
    const integrityMessage = integrityRows.map((r) => r.integrity_check).join("; ");
    const integrityOk = integrityMessage === "ok";

    const rowCountMismatches: RestoreVerification["rowCountMismatches"] = [];
    for (const [table, expected] of Object.entries(manifest.row_counts)) {
      const [row] = adapter.queryRaw(`SELECT COUNT(*) AS n FROM "${table}"`) as Array<{ n: number }>;
      const actual = row?.n ?? -1;
      if (actual !== expected) rowCountMismatches.push({ table, expected, actual });
    }

    return { integrityOk, integrityMessage, rowCountMismatches, ok: integrityOk && rowCountMismatches.length === 0 };
  } finally {
    adapter.close();
  }
}

const isMain = process.argv[1]?.endsWith("verify-restore.ts");
if (isMain) {
  const [snapshotPath, manifestPath] = process.argv.slice(2);
  if (!snapshotPath || !manifestPath) {
    console.error("usage: verify-restore.ts <snapshot-db-path> <manifest-json-path>");
    process.exit(1);
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const result = verifyRestore(snapshotPath, manifest);
  console.log(JSON.stringify(result));
  process.exit(result.ok ? 0 : 1);
}
