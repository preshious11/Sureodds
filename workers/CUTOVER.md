# Production cutover — SureOdds Cloudflare migration

Status: **preparation only**. Nothing below has been executed against
production. This is the exact sequence for when you decide to cut over,
plus rollback. It does not touch `wrangler.staging.toml`, staging D1, or
anything already running.

No subscriber-broadcast functionality is reintroduced anywhere in this
sequence — group delivery only (`workers/src/cron.ts`
`postGroupPicks`/`postGroupRecap`), matching production's
`SUBSCRIBER_DELIVERY_ENABLED=false`.

## 1. Production D1 setup

`wrangler.toml`'s `database_id` is still the placeholder
`REPLACE_WITH_STAGING_D1_ID` — production D1 has never been created.

```bash
cd workers
wrangler d1 create sureodds_production
# copy the returned database_id into wrangler.toml's [[d1_databases]] block
```

## 2. Apply migrations to production D1

Same six migrations staging already has, in order, against the new database:

```bash
cd workers
for f in migrations/0001_foundation.sql migrations/0002_update_retry.sql \
         migrations/0003_engine_state.sql migrations/0004_sync_state.sql \
         migrations/0005_ticket_candidates.sql migrations/0006_custom_sessions.sql; do
  wrangler d1 execute sureodds_production --remote --config wrangler.toml --file "$f"
done
```

## 3. Historical data requirement — do NOT copy staging data in

Per the explicit constraint: **never copy staging test data into
production.** Production D1 starts empty of `picks`/`prediction_memberships`/
`custom_pick_*`/`posts`/`deliveries` rows. The historical data it needs
instead comes from one place:

- **The production SQLite database** (`predictions-bot`'s live DB on the
  current JustRunMyApp host) is the actual historical record — settled
  picks, stats history, existing subscriber-irrelevant state. If you want
  production D1 to start with that history (so `/stats`/`/yesterday` don't
  start from zero on day one), export it with the same `d1_sync.py plan`
  pipeline already validated against staging, pointed at production
  instead:
  ```bash
  python3 workers/scripts/d1_sync.py plan <path-to-live-predbot.db> <path-to-live-predbot.db> \
    --out /tmp/prod_seed --tables picks,prediction_memberships,ticket_candidates,prediction_cache,custom_pick_candidates,custom_pick_pools \
    --write-budget 200000
  # review /tmp/prod_seed/plan.json before applying anything
  ```
  This has not been run. It requires the real production DB file, which
  this task's constraints correctly kept off-limits (no provider calls, no
  production access) — flagging it here as the one input only you can
  supply.
- If you'd rather start production D1 clean and let it accumulate its own
  history from cutover day forward, skip this step entirely — `/stats` and
  `/yesterday` will just show "No results yet" until the first day settles.

## 4. Secrets (production Worker)

```bash
cd workers
wrangler secret put TELEGRAM_BOT_TOKEN              # the SAME bot token production already uses
wrangler secret put TELEGRAM_WEBHOOK_SECRET         # a NEW random secret, not staging's
wrangler secret put TELEGRAM_CHAT_ID                # the real production group's chat id
wrangler secret put CLOUDFLARE_API_TOKEN            # for GitHub Actions' production workflow dispatch (if adding one)
wrangler secret put CLOUDFLARE_ACCOUNT_ID
```

`TELEGRAM_CHAT_ID` must be the exact group id the Python bot currently
posts to (`predbot/config.py`'s `TELEGRAM_CHAT_ID`) — reuse its value,
never a different chat, or group posts will fork into two destinations
during the overlap window in step 6.

## 5. Worker config review before first deploy

- `wrangler.toml`'s `[vars]` (`POST_TIME=09:00`, `DELIVERY_TIME=09:30`,
  `RECAP_TIME=07:45`, `TIMEZONE=Africa/Lagos`) already match production's
  `.env` defaults — confirm your production `.env` hasn't drifted from
  these before cutover (check live `POST_TIME`/`RECAP_TIME`/`TIMEZONE`).
- Cron triggers: **do not enable yet**. `[[triggers.crons]]` stays absent
  from `wrangler.toml` until step 7.
- `STAGE3_CRON_ENABLED`: **do not set to `"1"`** until step 7 either — both
  gates must flip together, same rule as staging.

## 6. Deploy the Worker (webhook NOT yet switched)

```bash
cd workers
wrangler deploy --config wrangler.toml
```

At this point the production Worker exists and can serve `/healthz` and
would accept webhook POSTs if pointed at it — but Telegram is still
sending updates to the Railway/JustRunMyApp poller, since its webhook (or
polling) hasn't been changed. This is the safe "deployed but not live"
state to verify from (`curl` the `/healthz` endpoint, confirm response).

## 7. Cut the webhook over (the actual go-live moment)

**Before this step**: stop the old host from being able to send group
messages or process updates concurrently with the new Worker — the
constraint "preventing old and new hosts from simultaneously processing
updates or sending group messages" means this must be a single moment,
not a window:

```bash
# 7a. Stop the old poller FIRST (JustRunMyApp container / process).
#     Confirm it is actually stopped (no live process) before 7b.

# 7b. Point Telegram at the new Worker.
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://sureodds-worker.<your-subdomain>.workers.dev/telegram/webhook" \
  -d "secret_token=<the TELEGRAM_WEBHOOK_SECRET you set in step 4>"

# 7c. Enable the Worker's own scheduling (now, not before).
#     Edit wrangler.toml: add STAGE3_CRON_ENABLED = "1" under [vars],
#     and add the [[triggers.crons]] block using the exact pattern
#     strings cron.ts exports (CRON_POST_SWEEP/CRON_RECAP_SWEEP/
#     CRON_DELIVER_SWEEP/CRON_WATCH) — copy wrangler.staging.toml's
#     commented block verbatim, uncommented, into wrangler.toml.
wrangler deploy --config wrangler.toml

# 7d. Enable the GitHub Actions generation schedule for production,
#     the same way staging's STAGE6_LIVE_ENABLED works: set the repo
#     variable for whichever workflow targets production generation
#     once that workflow exists (this task only built the staging one,
#     stage6-scheduled-staging.yml — a production equivalent pointed at
#     sureodds_production is new work, not yet written).
```

Verify immediately: `getWebhookInfo` shows the new URL, `/healthz` is
green, and the next scheduled post lands in the group exactly once (watch
the `posts` table for a single row, not a duplicate from the old host).

## 8. Disable JustRunMyApp (only after step 7 is confirmed working)

Once a full day's cycle (generation → group post → recap) has been
observed working correctly against production D1 with no duplicate posts
and no missed slate, stop/decommission the JustRunMyApp container. Keep
it stoppable-but-not-deleted for a few days in case of rollback.

## Rollback (tested path, not yet rehearsed against real production)

If anything in step 7 goes wrong:

```bash
# 1. Point the webhook back at nothing / clear it, so the new Worker stops
#    receiving updates.
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"

# 2. Restart the JustRunMyApp container/process (the old poller resumes
#    polling getUpdates once no webhook is set).

# 3. Immediately set STAGE3_CRON_ENABLED back to absent (or "0") in
#    wrangler.toml and `wrangler deploy` again, so the Worker's own
#    scheduling stops even if the webhook flip is briefly ambiguous.

# 4. Disable the production generation schedule's repo variable
#    (whatever step 7d's equivalent of STAGE6_LIVE_ENABLED ends up being),
#    so GitHub Actions stops generating against production D1 too.
```

Rollback does not require touching D1 data — the old host's SQLite DB was
never migrated away from (it's still there, untouched, if production D1
was seeded by copying rather than by a fresh start). If production D1 was
seeded from a live DB export (step 3) and real data was then written to
D1 only (not back to SQLite) during the live window, that window's data
exists only in D1 — decide before cutover whether that's acceptable, or
keep writing to both during a trial period.

## Required approvals before any of this runs for real

Everything in sections 1–8 needs your explicit go-ahead per this task's
constraints: switching the production webhook, deploying to the
production Worker, enabling production cron/schedule, and stopping the
existing production service are all listed as requiring explicit
approval. Nothing above has been executed.
