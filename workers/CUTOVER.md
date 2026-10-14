# Production cutover — SureOdds Cloudflare migration

Status: **preparation only**. Nothing below has been executed against
production. This is the exact, ordered sequence for when you decide to
cut over, plus rollback. It does not touch `wrangler.staging.toml`,
staging D1, or anything already running. Commands are Windows PowerShell
(this repo's working environment); adjust trivially for a Unix shell if
you ever run this from elsewhere.

No subscriber-broadcast functionality is reintroduced anywhere in this
sequence — group delivery only (`workers/src/cron.ts`
`postGroupPicks`/`postGroupRecap`), matching production's
`SUBSCRIBER_DELIVERY_ENABLED=false`.

## 1. Preflight verification

Before touching anything production-side, confirm on `main`:

```powershell
git log --oneline -1                                          # expect the commit this report names
cd workers; node --test tests/*.test.mjs; cd ..                # expect 100% pass
cd predictions-bot; .venv/Scripts/python.exe -m pytest tests -q; cd ..   # expect 100% pass
```

Confirm staging is healthy (proof the code itself works before pointing
it at production):

```powershell
Invoke-RestMethod https://<staging-worker>.workers.dev/healthz
```

and that staging's own `/stats`, `/top`, `/daily`, `/menu`, and a Custom
Picks request (group button → private deep link → request → ticket) all
behave as expected in a staging Telegram chat.

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

```powershell
cd workers
wrangler d1 create sureodds_production
# copy the returned database_id into wrangler.toml's [[d1_databases]] block, replacing REPLACE_WITH_STAGING_D1_ID

$migrations = @(
  "migrations/0001_foundation.sql", "migrations/0002_update_retry.sql",
  "migrations/0003_engine_state.sql", "migrations/0004_sync_state.sql",
  "migrations/0005_ticket_candidates.sql", "migrations/0006_custom_sessions.sql",
  "migrations/0007_custom_picks_conversation.sql"
)
foreach ($f in $migrations) {
  wrangler d1 execute sureodds_production --remote --config wrangler.toml --file $f
}
```

## 3. Historical-data requirements — do NOT copy staging data in

Per the explicit constraint: **never copy staging test data into
production.** Production D1 starts empty of `picks`/`prediction_memberships`/
`custom_pick_*`/`posts`/`deliveries` rows. There are exactly two ways to
give it history, and one input only you can supply:

**Option A — seed from the real production SQLite database** (the
`predictions-bot` live DB on the current JustRunMyApp host). This repo's
own `d1_sync.py` pipeline (already validated, not newly written) can plan
and apply an export of that file — `plan` is read-only against whatever
file you point it at; nothing is sent anywhere until the generated SQL is
applied with `wrangler d1 execute`:

```powershell
# Run this FROM the host that has the live predbot.db (or copy the file
# somewhere you can run Python against it — the file itself never leaves
# your control, and this command makes no network call).
python workers/scripts/d1_sync.py plan <path-to-live-predbot.db> <path-to-live-predbot.db> `
  --out C:\temp\prod_seed `
  --tables picks,prediction_memberships,ticket_candidates,prediction_cache,custom_pick_candidates,custom_pick_pools `
  --write-budget 200000
# Review C:\temp\prod_seed\plan.json BEFORE applying anything — it lists
# every row the plan would write, so you can confirm it's historical data
# only, nothing fabricated. Nothing has been written yet.

# Only once you've reviewed the plan, apply each generated batch file:
Get-ChildItem C:\temp\prod_seed\p0_*.sql, C:\temp\prod_seed\p1_*.sql | ForEach-Object {
  wrangler d1 execute sureodds_production --remote --config workers/wrangler.toml --file $_.FullName
}
```

Not run this pass — it requires the real production DB file, which this
task's constraints correctly kept off-limits (no provider calls, no
production access).

**Data that cannot be migrated this way, even with the file in hand:**
- **Individual subscriber rows** (the `subscribers` table) — not applicable:
  subscriber broadcasting is disabled in both production and this port, so
  there is nothing meaningful to carry over (and doing so would risk
  re-enabling a delivery path that's supposed to stay off).
- **`posts`/`deliveries` receipts** — deliberately excluded from the
  `--tables` list above. Carrying over old receipt rows would make the new
  Worker think today's cards are already posted when they aren't (or, if
  the dates don't collide, they're just dead rows). Let the new system
  write its own receipts from cutover day forward.
- **In-flight Custom Picks UI state** (conversation refinement, pending
  suggestion tokens) — inherently ephemeral (10-minute TTL in both
  production's in-memory dicts and this port's D1 tables); there is
  nothing meaningful to migrate, it simply starts empty.
- **Anything written to the live SQLite file AFTER you run the export** —
  this is a point-in-time snapshot. If generation keeps running on the old
  host after you've exported, that gap is not re-synced automatically; plan
  the export for as close to the overlap-prevention step (step 7) as
  practical.

**Option B — start production D1 clean.** Skip this step entirely —
`/stats` will show "No results yet" and the 7-day trend will show "Last 1
day" growing daily, until a week has passed. Both are honest, not broken,
outputs; nothing is fabricated to fill the gap.

## 4. Production Worker deployment

Deploy with the webhook still pointed at the old host — this proves the
Worker runs before anything depends on it:

```powershell
cd workers
wrangler deploy --config wrangler.toml
Invoke-RestMethod https://<your-production-worker>.workers.dev/healthz   # expect ok: true
```

At this point Telegram is still sending updates to JustRunMyApp; the new
Worker exists but is not yet receiving anything.

## 5. Production secrets and bindings verification

```powershell
cd workers
wrangler secret put TELEGRAM_BOT_TOKEN              # the SAME token production already uses
wrangler secret put TELEGRAM_WEBHOOK_SECRET         # a NEW random secret, not staging's
wrangler secret put TELEGRAM_CHAT_ID                # the real production group's chat id
wrangler secret put CLOUDFLARE_API_TOKEN            # for wrangler itself / future tooling
wrangler secret put CLOUDFLARE_ACCOUNT_ID
# For the production generation workflow (step 6), set these as GitHub
# repo secrets instead (Settings > Secrets and variables > Actions):
#   PRODUCTION_CLOUDFLARE_API_TOKEN, PRODUCTION_CLOUDFLARE_ACCOUNT_ID,
#   PRODUCTION_TELEGRAM_BOT_TOKEN, PRODUCTION_TELEGRAM_CHAT_ID
# (separate names from the staging ones already in use, so a workflow
# typo can never cross-wire staging and production secrets.)
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

## 6. Production generation workflow preparation — DONE this pass

`stage5-acceptance-production.yml` (new) is a parallel pipeline to
staging's `stage5-acceptance.yml`, not a parameterized version of it —
the staging file's "Staging target guard" exists specifically so that
pipeline can never be pointed at production, and weakening it to accept a
target parameter would undo the one thing that guard is for. The new file
has its own "Production target guard" (asserts `wrangler.toml` names
`sureodds_production` and no longer has the placeholder database id) and
reads a separate secret namespace (`PRODUCTION_*`, see step 5).
`stage7-scheduled-production.yml` (new) is the daily trigger, same
safe-by-default pattern as staging's `stage6-scheduled-staging.yml`: a
schedule fire always runs `operation=fixture-replay`/`dry_run=true`
(offline, zero provider calls, zero D1 writes) **unless** the repo
variable `STAGE7_LIVE_ENABLED` is set to exactly `"true"`. Nothing in this
pass sets that variable — production generation stays off until you flip
it, which only makes sense once step 2 has created real production D1
(the guard refuses to run otherwise).

## 7. Prevention of overlapping old/new bot processes

This is the step that prevents duplicate group messages and duplicate
update processing — it must happen as a single moment, not a window, and
in this exact order:

```powershell
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

```powershell
$token = "<production bot token>"
$secret = "<the TELEGRAM_WEBHOOK_SECRET from step 5>"
Invoke-RestMethod "https://api.telegram.org/bot$token/setWebhook" -Method Post -Body @{
  url = "https://<your-production-worker>.workers.dev/telegram/webhook"
  secret_token = $secret
}
Invoke-RestMethod "https://api.telegram.org/bot$token/getWebhookInfo"   # confirm the new URL, no pending_update_count spike
```

## 9. Command and Custom Picks verification

In the production Telegram group and in a private chat with the bot,
confirm (against production D1, so with whatever history step 3 did or
didn't seed):
- `/top`, `/daily`, `/all`, `/stats`, `/yesterday`, `/stats_top`,
  `/stats_daily` all render correctly.
- `/menu` (as an admin) posts the community menu, with the Custom Picks
  button, to the group — and a non-admin gets "Not allowed."
- Tapping the Custom Picks button opens a private chat and sends the
  exact INTRO text.
- A request in that private chat (e.g. "3 picks") returns a ticket if
  today's archive pool is ready, or the honest "Still cooking" message
  if not — never a fabricated ticket. A request that can't be satisfied
  offers a real, tappable alternative; tapping it delivers an actual
  ticket, not a dead end.
- `/start`, `/stop` correctly leave an active Custom Picks session.

## 10. Group posting verification

Before enabling the schedule (step 11), verify the group-post PATH itself
with a single manual, controlled trigger if possible, or by close
monitoring on the first automatic run:
- The automatic post is one message per card (Top, then Daily), the
  WHOLE ticket, never paginated, never with a stray "Page 1 of N" footer.
- Non-empty cards carry no keyboard. A genuinely-empty Top card carries
  the Custom Picks button only if `TELEGRAM_BOT_USERNAME` is set.
- Watch the `posts` table for exactly one row per kind:
  ```powershell
  wrangler d1 execute sureodds_production --remote --config workers/wrangler.toml --json `
    --command "SELECT * FROM posts WHERE post_date = date('now')"
  ```
  a second host still running would show as a duplicate attempt here
  before it shows as a duplicate message.

## 11. Activation of automatic generation and Cron triggers

Only after steps 7–10 are all confirmed working:

```powershell
# Worker-side scheduling: edit workers/wrangler.toml, add under [vars]:
#   STAGE3_CRON_ENABLED = "1"
# and add the [[triggers.crons]] block using the exact pattern strings
# cron.ts exports (CRON_POST_SWEEP/CRON_RECAP_SWEEP/CRON_DELIVER_SWEEP/
# CRON_WATCH) — copy wrangler.staging.toml's commented block verbatim,
# uncommented, into wrangler.toml.
cd workers
wrangler deploy --config wrangler.toml

# Generation scheduling: set the GitHub repo variable (Settings > Secrets
# and variables > Actions > Variables):
#   STAGE7_LIVE_ENABLED = true
# stage7-scheduled-production.yml then runs live generation daily at
# 09:00 Lagos with no further workflow edits needed.
```

## 12. Final verification

A full day's cycle, observed end to end against production D1:
generation lands (COMPLETE cache) → Top/Daily post once each, whole
ticket, correct keyboard rule → recap posts once at 07:45 Lagos the next
day → `/stats`/`/yesterday`/`/stats_top`/`/stats_daily`/`/menu`/Custom
Picks all correct → no duplicate rows in `posts`/`deliveries` → no errors
in Worker logs:

```powershell
wrangler tail --config workers/wrangler.toml
```

## 13. Shutdown of JustRunMyApp

Only after step 12 passes for at least one full cycle. Keep it
stoppable-but-not-deleted for a few days in case of rollback — don't
decommission the container/image, just stop the running process.

## 14. Rollback if any critical step fails

```powershell
$token = "<production bot token>"
# 1. Point the webhook back at nothing, so the new Worker stops receiving updates.
Invoke-RestMethod "https://api.telegram.org/bot$token/deleteWebhook" -Method Post

# 2. Restart the JustRunMyApp container/process (it resumes polling
#    getUpdates once no webhook is set).

# 3. Immediately set STAGE3_CRON_ENABLED back to absent (or "0") in
#    workers/wrangler.toml and redeploy, so the Worker's own scheduling
#    stops even if the webhook flip is briefly ambiguous.
cd workers
wrangler deploy --config wrangler.toml

# 4. If step 6/11's generation schedule was ever enabled, disable it:
#    set the repo variable STAGE7_LIVE_ENABLED back to false (or delete
#    it), so GitHub Actions stops generating against production D1.
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
