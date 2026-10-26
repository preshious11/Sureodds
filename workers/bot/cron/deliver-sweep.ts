// systemd timer entry point for the former CRON_DELIVER_SWEEP trigger
// ("*/5 * * * *" - every 5 minutes, any time). See post-sweep.ts's header
// comment - same pattern, different cron constant. This is the sweep that
// dispatches the GitHub Actions generation/settlement/live-monitor
// workflows on demand (maybeDispatchGeneration et al., unchanged) - it
// needs GITHUB_DISPATCH_TOKEN in .env to do that part of its job.
import { handleScheduled, CRON_DELIVER_SWEEP } from "../../src/cron.ts";
import { requireEnvVars, buildServerEnv } from "../server-env.ts";

requireEnvVars(["TELEGRAM_BOT_TOKEN", "SQLITE_DB_PATH"]);
const env = buildServerEnv();

handleScheduled(env, CRON_DELIVER_SWEEP, Date.now())
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(JSON.stringify({ event: "deliver_sweep_failed", error: err instanceof Error ? err.message : String(err) }));
    process.exit(1);
  });
