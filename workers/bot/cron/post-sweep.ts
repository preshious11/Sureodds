// systemd timer entry point for the Cloudflare Worker's former
// CRON_POST_SWEEP trigger ("*/5 8-22 * * *" - 09:00-23:55 Lagos). Calls the
// SAME handleScheduled() from workers/src/cron.ts, unmodified - only the
// trigger mechanism (systemd timer instead of a Cloudflare Cron Trigger)
// and the database (LocalSqliteAdapter) differ.
import { handleScheduled, CRON_POST_SWEEP } from "../../src/cron.ts";
import { requireEnvVars, buildServerEnv } from "../server-env.ts";

requireEnvVars(["TELEGRAM_BOT_TOKEN", "SQLITE_DB_PATH"]);
const env = buildServerEnv();

handleScheduled(env, CRON_POST_SWEEP, Date.now())
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(JSON.stringify({ event: "post_sweep_failed", error: err instanceof Error ? err.message : String(err) }));
    process.exit(1);
  });
