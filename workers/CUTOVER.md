# Production cutover — SureOdds Cloudflare migration

Status: **preparation only**. Nothing below has been executed against
production. This is the exact, ordered sequence for when you decide to
cut over, plus rollback. It does not touch `wrangler.staging.toml`,
staging D1, or anything already running.

No subscriber-broadcast functionality is reintroduced anywhere in this
sequence — group delivery only (`workers/src/cron.ts`
`postGroupPicks`/`postGroupRecap`), matching production's
`SUBSCRIBER_DELIVERY_ENABLED=false`.

## 1. Preflight verification

Before touching anything production-side, confirm on `main`:

```bash
git log --oneline -1                                  # expect the commit this report names
cd workers && node --test tests/*.test.mjs             # expect 100% pass
cd ../predictions-bot && python -m pytest tests -q     # expect 100% pass
```

Confirm staging is healthy (proof the code itself works before pointing
it at production): `curl https://<staging-worker>.workers.dev/healthz`,
and that staging's own `/stats`, `/top`, `/daily`, `/menu`, and a Custom
Picks request all behave as expected in a staging Telegram chat.

Confirm the values you'll need, without printing any secret:
- Production bot token is the SAME token the Python bot already uses
  (reusing production's own token, not a new one).
- The production Telegram group's chat id.
- The bot's `@username` (for `TELEGRAM_BOT_USERNAME` — needed for the
  Custom Picks deep-link button to ever appear).
- Which admin Telegram user ids should be allowed to run `/menu`
  (`TELEGRAM_ADMIN_IDS`).

## 2. Production D1 setup and migrations

`wrangler.toml`'s `database_id` is still the placeholder
`REPLACE_WITH_STAGING_D1_ID` — production D1 has never been created.

```bash
cd workers
wrangler d1 create sureodds_production
# copy the returned database_id into wrangler.toml's [[d1_databases]] block

for f in migrations/0001_foundation.sql migrations/0002_update_retry.sql \
         migrations/0003_engine_state.sql migrations/0004_sync_state.sql \
         migrations/0005_ticket_candidates.sql migrations/0006_custom_sessions.sql; do
  wrangler d1 execute sureodds_production --remote --config wrangler.toml --file "$f"
done
```

## 3. Historical-data requirements — do NOT copy staging data in

Per the explicit constraint: **never copy staging test data into
production.** Production D1 starts empty of `picks`/`prediction_memberships`/
`custom_pick_*`/`posts`/`deliveries` rows. The historical data it needs
instead comes from one place:

- **The production SQLite database** (`predictions-bot`'s live DB on the
  current JustRunMyApp host) is the actual historical record. If you want
  production D1 to start with that history (so `/stats`/`/yesterday`/the
  7-day trend don't start from zero on day one), export it with the same
  `d1_sync.py plan` pipeline already validated against staging, pointed at
  production instead:
  ```bash
  python3 workers/scripts/d1_sync.py plan <path-to-live-predbot.db> <path-to-live-predbot.db> \
    --out /tmp/prod_seed --tables picks,prediction_memberships,ticket_candidates,prediction_cache,custom_pick_candidates,custom_pick_pools \
    --write-budget 200000
  # review /tmp/prod_seed/plan.json before applying anything
  ```
  Not run — it requires the real production DB file, which this task's
  constraints correctly kept off-limits (no provider calls, no production
  access). This is the one input only you can supply.
- If you'd rather start production D1 clean and let it accumulate its own
  history from cutover day forward, skip this step — `/stats` will show
  "No results yet" and the 7-day trend will show "Last 1 day" growing
  daily, until a week has passed. Both are honest, not broken.

## 4. Production Worker deployment

Deploy with the webhook still pointed at the old host — this proves the
Worker runs before anything depends on it:

```bash
cd workers
wrangler deploy --config wrangler.toml
curl https://<your-production-worker>.workers.dev/healthz   # expect {"ok":true,...}
```

At this point Telegram is still sending updates to JustRunMyApp; the new
Worker exists but is not yet receiving anything.

## 5. Production secrets and bindings verification

```bash
cd workers
wrangler secret put TELEGRAM_BOT_TOKEN              # the SAME token production already uses
wrangler secret put TELEGRAM_WEBHOOK_SECRET         # a NEW random secret, not staging's
wrangler secret put TELEGRAM_CHAT_ID                # the real production group's chat id
wrangler secret put CLOUDFLARE_API_TOKEN            # for the production generation workflow (step 6)
wrangler secret put CLOUDFLARE_ACCOUNT_ID
wrangler secret list --config wrangler.toml         # verify names only — never prints values
```

Also set in `wrangler.toml`'s `[vars]` (not secrets, but verify before
deploying again): `TELEGRAM_BOT_USERNAME` (your bot's `@handle`, no `@`)
and `TELEGRAM_ADMIN_IDS` (comma-separated numeric Telegram user ids).
Without `TELEGRAM_BOT_USERNAME` set, the Custom Picks button is silently
omitted everywhere (never a dead link) — confirm it's actually set if you
want that button live.

`TELEGRAM_CHAT_ID` must be the exact group id the Python bot currently
posts to (`predbot/config.py`'s `TELEGRAM_CHAT_ID`) — reuse its value,
never a different chat, or group posts fork into two destinations during
the overlap window in step 7.

## 6. Production generation workflow preparation — BLOCKER, flagged explicitly

**Not done, and deliberately not rushed.** `stage5-acceptance.yml`'s own
"Staging target guard" step hardcodes `STAGING_DB_NAME=sureodds_staging`
and asserts `wrangler.staging.toml` contains it, specifically so this
pipeline can never be accidentally pointed at production. That guard is
exactly right and should not be weakened. A production-equivalent of
`stage6-scheduled-staging.yml` (a `stage7-scheduled-production.yml`
calling a production-target version of the acceptance pipeline) is new
work this pass did not attempt, because writing it carelessly is the
single easiest way to create a path that writes to production by
mistake. This needs its own careful pass: either a parameterized version
of `stage5-acceptance.yml` that takes the target database as an explicit,
guarded input, or a deliberately separate, reviewed `stage7` file. Until
it exists, production generation has no automatic path — treat this as a
MEDIUM-severity blocker (see final report, item K) rather than silently
skipped.

## 7. Prevention of overlapping old/new bot processes

This is the step that prevents duplicate group messages and duplicate
update processing — it must happen as a single moment, not a window, and
in this exact order:

```bash
# 7a. Stop the old poller FIRST (JustRunMyApp container / process).
#     Confirm it is actually stopped — no live process — before 7b.
#     While it's stopped, Telegram queues updates; nothing is lost.

# 7b. Only after 7a is confirmed: proceed to step 8 (webhook switch).
```

Do not perform step 8 while the old poller might still be running —
Telegram would then be delivering the same updates to both hosts, and
both could post the same group message independently (neither host's
dedup/receipt tables know about the other).

## 8. Telegram webhook switch

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://<your-production-worker>.workers.dev/telegram/webhook" \
  -d "secret_token=<the TELEGRAM_WEBHOOK_SECRET from step 5>"
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"   # confirm the new URL, no pending_update_count spike
```

## 9. Command and Custom Picks verification

In the production Telegram group and in a private chat with the bot,
confirm (against production D1, so with whatever history step 3 did or
didn't seed):
- `/top`, `/daily`, `/all`, `/stats`, `/yesterday` all render correctly.
- `/menu` (as an admin) posts the community menu, with the Custom Picks
  button, to the group — and a non-admin gets "Not allowed."
- Tapping the Custom Picks button opens a private chat and sends the
  exact INTRO text.
- A request in that private chat (e.g. "3 picks") returns a ticket if
  today's archive pool is ready, or the honest "Still cooking" message
  if not — never a fabricated ticket.
- `/start`, `/stop` correctly leave an active Custom Picks session.

## 10. Group posting verification

Before enabling the schedule (step 11), verify the group-post PATH itself
with a single manual, controlled trigger if possible, or by close
monitoring on the first automatic run:
- The automatic post is one message per card (Top, then Daily), the
  WHOLE ticket, never paginated, never with a stray "Page 1 of N" footer.
- Non-empty cards carry no keyboard. A genuinely-empty Top card carries
  the Custom Picks button only if `TELEGRAM_BOT_USERNAME` is set.
- Watch the `posts` table (`SELECT * FROM posts WHERE post_date = <today>`)
  for exactly one row per kind — a second host still running would show
  as a duplicate attempt here before it shows as a duplicate message.

## 11. Activation of automatic generation and Cron triggers

Only after steps 7–10 are all confirmed working:

```bash
# Worker-side scheduling: edit wrangler.toml, add under [vars]:
#   STAGE3_CRON_ENABLED = "1"
# and add the [[triggers.crons]] block using the exact pattern strings
# cron.ts exports (CRON_POST_SWEEP/CRON_RECAP_SWEEP/CRON_DELIVER_SWEEP/
# CRON_WATCH) — copy wrangler.staging.toml's commented block verbatim,
# uncommented, into wrangler.toml.
wrangler deploy --config wrangler.toml

# Generation scheduling: blocked on step 6 (stage7-scheduled-production.yml
# doesn't exist yet). Until it does, generation must be run manually
# (predictions-bot's existing CLI/run_once.py against production D1, with
# the same care stage5-acceptance.yml's guards already encode) or step 6
# must be completed first.
```

## 12. Final verification

A full day's cycle, observed end to end against production D1:
generation lands (COMPLETE cache) → Top/Daily post once each, whole
ticket, correct keyboard rule → recap posts once at 07:45 Lagos the next
day → `/stats`/`/yesterday`/`/menu`/Custom Picks all correct → no
duplicate rows in `posts`/`deliveries` → no errors in Worker logs
(`wrangler tail --config wrangler.toml`).

## 13. Shutdown of JustRunMyApp

Only after step 12 passes for at least one full cycle. Keep it
stoppable-but-not-deleted for a few days in case of rollback — don't
decommission the container/image, just stop the running process.

## 14. Rollback if any critical step fails

```bash
# 1. Point the webhook back at nothing, so the new Worker stops receiving updates.
curl -X POST "https://api.telegram.org/bot<TOKEN>/deleteWebhook"

# 2. Restart the JustRunMyApp container/process (it resumes polling
#    getUpdates once no webhook is set).

# 3. Immediately set STAGE3_CRON_ENABLED back to absent (or "0") in
#    wrangler.toml and `wrangler deploy` again, so the Worker's own
#    scheduling stops even if the webhook flip is briefly ambiguous.

# 4. If step 6/11's generation schedule was ever enabled, disable its
#    repo variable too, so GitHub Actions stops generating against
#    production D1.
```

Rollback does not require touching D1 data — the old host's SQLite DB was
never migrated away from (still there, untouched, if production D1 was
seeded by copying rather than a fresh start in step 3). If production D1
was seeded from a live DB export and then written to independently during
a live window before rollback, that window's writes exist only in D1 —
decide before cutover whether that's acceptable, or write to both during
a trial period.

## Required approvals before any of this runs for real

Everything above needs your explicit go-ahead per this task's
constraints: switching the production webhook, deploying to the
production Worker, enabling production cron/schedule, and stopping the
existing production service all require explicit approval. Nothing above
has been executed.
