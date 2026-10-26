// CLI wrapper printing the live database's current sync_state.epoch, for
// the GitHub Actions workflow to read immediately before building its plan
// manifest - mirrors exactly what the existing D1 apply step already does
// (reads epoch right before applying, bumps it with a WHERE epoch=$epoch
// CAS right after). Invoked by receive-batch.sh's "epoch-get" job, which
// is read-only and does not expect a tarred batch on stdin.
//
// Usage: node get-epoch.ts <sqlite-db-path>
import { LocalSqliteAdapter } from "../local-sqlite-adapter.ts";

const [dbPath] = process.argv.slice(2);
if (!dbPath) {
  console.error("usage: get-epoch.ts <sqlite-db-path>");
  process.exit(1);
}

const adapter = new LocalSqliteAdapter(dbPath);
try {
  console.log(JSON.stringify({ epoch: adapter.currentEpoch() }));
  process.exit(0);
} finally {
  adapter.close();
}
