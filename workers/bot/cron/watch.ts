// systemd timer entry point for the former CRON_WATCH trigger
// ("0 10 * * *" - once daily, 11:00 Lagos). See post-sweep.ts's header
// comment - same pattern, different cron constant.
import { handleScheduled, CRON_WATCH } from "../../src/cron.ts";
import { requireEnvVars, buildServerEnv } from "../server-env.ts";

requireEnvVars(["TELEGRAM_BOT_TOKEN", "SQLITE_DB_PATH"]);
const env = buildServerEnv();

handleScheduled(env, CRON_WATCH, Date.now())
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(JSON.stringify({ event: "watch_failed", error: err instanceof Error ? err.message : String(err) }));
    process.exit(1);
  });
