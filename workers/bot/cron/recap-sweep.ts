// systemd timer entry point for the former CRON_RECAP_SWEEP trigger
// ("*/5 6-7 * * *" - 07:00-08:55 Lagos). See post-sweep.ts's header comment -
// same pattern, different cron constant.
import { handleScheduled, CRON_RECAP_SWEEP } from "../../src/cron.ts";
import { requireEnvVars, buildServerEnv } from "../server-env.ts";

requireEnvVars(["TELEGRAM_BOT_TOKEN", "SQLITE_DB_PATH"]);
const env = buildServerEnv();

handleScheduled(env, CRON_RECAP_SWEEP, Date.now())
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(JSON.stringify({ event: "recap_sweep_failed", error: err instanceof Error ? err.message : String(err) }));
    process.exit(1);
  });
