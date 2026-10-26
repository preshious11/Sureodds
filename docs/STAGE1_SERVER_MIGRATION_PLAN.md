# Server Migration Plan: Telegram Bot + Database + Crons off Cloudflare

**Supersedes** `STAGE1_IMPLEMENTATION_PLAN.md`, `WORKER_DEPENDENCIES_INVENTORY.md`,
`STAGE1_BUILD_CHECKLIST.md`, and `STAGE1_STAGING_RUNBOOK.md` on
`feature/stage1-telegram-polling`: the D1 REST adapter built there is not
the target any more. This plan replaces D1 entirely with a local SQLite
file on the Oracle server. Nothing from the old branch is reused except the
one finding that still holds: Node 24 runs `.ts` files directly with no
build step, importing `workers/src/*.ts` unmodified.

Branch: `feature/stage1-server-migration`. Nothing here is executed yet —
this is stop point 1, the written plan.

## Build status (stop point 2 addendum)

Built and tested (41 new tests, all passing; the existing Worker suite's
572 tests unaffected):

- `sqlite-driver.ts` + `local-sqlite-adapter.ts`: the D1-compatible
  adapter, `batch()` as a real transaction, `applyPlan()` for idempotent
  delivery
- `bot.ts`: ported from the superseded D1-REST-adapter branch, adapter
  swapped, quota-backoff/admin-alert logic retained (now dormant in
  practice - no free-tier quota on a local file - but kept rather than
  removed, since `db.ts` is unmodified and could still throw it)
- The 4 cron entry scripts (`cron/post-sweep.ts` etc.) + systemd
  `.service`/`.timer` pairs, each with `flock` locking
- The Pro closing job's systemd unit (Python script unchanged)
- The SSH relay: `verify-manifest.ts` (checksum verification),
  `apply-plan.ts` (idempotent single-transaction apply), `receive-batch.sh`
  (the forced-command target), `deliver-batch.sh` (the Actions-side
  sender, with host key pinning)
- `build_plan_manifest.py`: converts `d1_sync.py`'s existing SQL batch
  output into the manifest the relay expects (a thin adapter, `d1_sync.py`
  itself is untouched)
- Backup: `backup.sh` (VACUUM INTO, instance-principal-first with an
  API-key fallback) + `verify-restore.ts` (the tested-restore check) +
  systemd unit

**Closed (was the stop-point-2 gap):** stage5/9/10/11 are now wired to the
SSH relay behind a `write_target` input (`d1` default, `server`
rehearsal-only), added to every production-writing workflow's
`workflow_dispatch` inputs. The epoch question that blocked this is
answered in full above the build-status section: no reader
(bot/crons/caches) ever reads `sync_state.epoch`; it is a writer-vs-writer
staleness fence only, and D1's own apply/rest-apply/epoch-bump sequence is
three separate, non-transactional steps precisely because D1's REST API
can't do better. The server's `applyPlan()` closes that gap - data apply
and epoch bump now commit together, in one SQLite transaction, which D1
structurally cannot do.

Each workflow's existing "Apply to production D1" step gained exactly one
line (its `if:` condition now also requires `write_target == 'd1'`,
defaulting true) - a new, separately-guarded "Apply to server" step runs
instead when `write_target == 'server'`, reachable only via an explicit
`workflow_dispatch`; scheduled/`workflow_call` runs can never set it, so
the default path is unaffected. `rest/*.jsonl` entries (D1's oversized-row
path) are handled too, via `build_plan_manifest.py --rest-dir` and
`applyPlan()`'s new `paramStatements` argument - the server target is
feature-complete against what production's plan step actually produces,
not just the common case.

**Also confirmed, not yet decided:**
- `node:sqlite` is Stability 1.2 (Release Candidate) as of Node 24.15 -
  no longer behind a flag, not yet "2 - Stable". Preferred over
  better-sqlite3 because it has no native binary (nothing to rebuild
  against this Node ABI on a 1 GB box with no build toolchain by default)
  and is already what the Worker's 572 existing tests run against on this
  exact Node version. `sqlite-driver.ts` is the one file naming a concrete
  library - swapping to better-sqlite3 if RC status ever regresses in
  practice is a one-file change there.
- Oracle Object Storage Always Free limit is in the 10-20 GB range across
  sources that disagree slightly - confirm the exact figure on your own
  tenancy dashboard; a SQLite backup here is small enough that either
  figure leaves ample room.
- Instance principals (dynamic group + policy) are a standard OCI IAM
  feature, not something gated by the Always Free tier - `backup.sh` tries
  instance-principal auth first and only falls back to an API key if that
  isn't set up yet. The dynamic group/policy themselves are a one-time OCI
  console step on your side, not something this repo can configure.
- Peak memory is now an explicit pass/fail rehearsal item (not just an
  estimate) - see the updated §9 below.

---

## 1. What moves, what stays

| Component | Today | After cutover |
|---|---|---|
| Telegram bot (webhook) | Cloudflare Worker, inbound webhook | Oracle server, long polling (`getUpdates`) |
| Database | Cloudflare D1 (`sureodds_production`) | SQLite file on the Oracle server |
| 4 Worker crons (post/recap/deliver/watch sweeps) | Cloudflare Cron Triggers | systemd timers on the server |
| Pro closing capture (30-min) | GitHub Actions cron (`pro-value-closing-shadow.yml`) | systemd timer on the server |
| Generation (picks) | GitHub Actions, writes to D1 via REST | GitHub Actions (unchanged), delivers to the server over SSH instead of D1 |
| Settlement (won/lost) | GitHub Actions, writes to D1 via REST | Same: SSH delivery instead of D1 REST |
| History backfill, closing-price backfill | GitHub Actions, writes to D1 via REST | Same: SSH delivery instead of D1 REST |
| Backups | Cloudflare D1's own durability | Scheduled SQLite snapshots to Oracle Object Storage |

Not moving yet (explicitly deferred, per your instruction): removing
match-start/end notifications, measuring Actions minutes, making the repo
private. These happen after cutover, as separate steps.

---

## 2. A finding that changes the shape of this plan

`workers/scripts/d1_sync.py` — the script every production-writing GitHub
Actions workflow already uses — is **not D1-specific internally**. It has
two independent halves:

1. **`plan`** (and `rebase`, `push`, `manifest`): diffs a local SQLite
   scratch database (built during generation/settlement) against a base
   snapshot, and writes the difference as a batch of files. Pure SQLite
   logic, no Cloudflare involved.
2. **The apply step**, which today is `rest-apply` — it takes the batch
   from step 1 and pushes it to Cloudflare D1 over REST. But the script
   **already has a second apply command, `apply`**, that takes the exact
   same kind of batch (as `.sql` files instead of REST JSON) and applies
   it to **a local SQLite file** (`sqlite3.connect(target)`), inside one
   transaction per batch file, with its own epoch check-and-set and a
   resumable `applied.json` checkpoint. This exists today for staging
   sync (`stage4c-staging-sync.yml`) — it has just never been pointed at
   a server over SSH instead of a local path in the same job.

This means the generation/settlement delivery mechanism in this plan is
**not new code**: swap the workflows' `rest-apply` step for (a) producing
the same batch as `.sql` files (the `plan`/`push` commands already support
this output shape) and (b) transferring that batch directory to the
server and running the **existing** `d1_sync.py apply` command there,
unmodified, against the server's SQLite file. The only new code is the
transfer itself (the forced-command SSH relay, §6) and a thin wrapper that
invokes `apply` after a successful transfer.

The same reasoning applies to the bot and cron side: Cloudflare D1 **is**
SQLite under the hood, and the SQL in every migration file and every
`.prepare()` call is already plain SQLite syntax — there is no dialect
translation step anywhere in this plan.

And the local SQLite adapter itself is not a new design either:
`workers/tests/access_and_result_gifs.test.mjs` (and every other Worker
test file) already defines exactly this adapter inline, using Node's
built-in `node:sqlite` module, to mock `env.DB` for 572 passing tests
today:

```js
const DB = { prepare(query) { return {
  args: [],
  bind(...a) { this.args = a; return this; },
  async first() { return sql.prepare(query).get(...this.args) ?? null; },
  async all() { return { results: sql.prepare(query).all(...this.args) }; },
  async run() { return { meta: { changes: sql.prepare(query).run(...this.args).changes } }; },
}; } };
```

This plan productionizes that exact pattern into a shared module
(`local-sqlite-adapter.ts`), adds `batch()` as a transaction (not present
in the test mock, needed because the real D1 binding's `batch()` is
atomic and this is now the permanent adapter, not a bot-only shim), and
adds WAL mode + busy-timeout for concurrent access (§5). No third-party
SQLite package — `node:sqlite` is built into Node 24.

---

## 3. Complete inventory: every D1 read/write site today, and its SQLite path

### 3a. Worker source (`workers/src/`) — becomes the bot + cron timers

| File | What it does with D1 | How it reaches SQLite |
|---|---|---|
| `db.ts` | ~50 `.prepare()` calls: update dedupe, subscribers, day cache, custom sessions, suggestions, pinned menu, job state, match events, result GIFs | Unmodified — calls `env.DB.prepare()...`, where `env.DB` is now `LocalSqliteAdapter` instead of the D1 binding or the old REST adapter |
| `cron.ts` | ~21 `.prepare()` calls: post/recap/match-event receipts, match-event alert state, Pro-picks ledger checks, failed-update reporting | Unmodified, same substitution. `handleScheduled()` is called by 5 new small entry scripts (one per cron kind + the closing job), each a systemd timer target |
| `index.ts` | ~8 `.prepare()`-adjacent calls inside `handleUpdate`/`routeUpdate` (routes through `db.ts` mostly; a few direct) | Unmodified — this is the bot's own entry point, already proven against a mocked adapter on the old branch |
| `ledger.ts` | 5 `.prepare()` calls: issued-ticket ledger inserts, settlement status updates | Unmodified |
| `ticket_image.ts` | 1 `.prepare()` call: reads fixtures for the Bet-of-the-Day image | Unmodified |
| `views.ts` | 0 direct `.prepare()` calls — reads through `db.ts` functions (`getAllPicksForCollection`, `getStatsRowsForDateByCollection`, etc.) | No change needed beyond `db.ts`'s own substitution |

**Nothing in `workers/src/` is rewritten.** Every file above keeps calling
`env.DB` exactly as written; only what `env.DB` *is* changes, from the D1
binding to `LocalSqliteAdapter`.

### 3b. GitHub Actions workflows — stay on Actions, change only the apply step

| Workflow | Current D1 write path | New path |
|---|---|---|
| `stage5-acceptance-production.yml` (generation) | `d1_sync.py plan` → `rest/` batch → `rest-apply` (REST to Cloudflare) | `d1_sync.py plan` → `.sql` batch → SSH transfer → `d1_sync.py apply` on the server |
| `stage9-settle-production.yml` (settlement, won/lost) | Same `plan`/`rest-apply` pattern | Same substitution |
| `stage10-history-backfill-production.yml` | Same pattern | Same substitution |
| `stage11-closing-price-production.yml` | Same pattern | Same substitution |
| `stage4c-staging-sync.yml` | Already uses local-file `apply` (staging only) | Unaffected — this workflow already does locally what production is moving to |
| `pro-value-closing-shadow.yml` | Python (`tools/pro_value_shadow.py closing`), writes to its own `/tmp/pro_value_shadow.db`, reads the archive read-only from a local scratch copy | Moves to a systemd timer on the server; the archive read becomes a direct read-only open of the live server SQLite file (`?mode=ro`, same code in `botd_archive.py`) — simpler than today, no scratch-copy step needed |
| `diag-readonly.yml`, `diag-telegram-webhook.yml`, `sporty-catalogue-probe.yml` | Read-only diagnostics against D1/the webhook | Out of scope for this migration (diagnostic tools, not part of the write path); revisited after cutover if still useful |

### 3c. Correction: subscriber broadcasting does not run today

An earlier version of this inventory listed `CRON_DELIVER_SWEEP` (the
5-minute cron) as broadcasting daily picks and recaps to individual
subscriber chat IDs. That was wrong. Reading `handleScheduled()` in
`workers/src/cron.ts` directly:

- `CRON_DELIVER_SWEEP` calls `reportFailedUpdates`, `sweepMatchEvents`
  (match-start/match-end events, delivered only to the group + owner —
  independent receipts, never a subscriber row), and the three
  `maybeDispatch*` functions (these only trigger GitHub Actions
  workflows, no Telegram send at all).
- `CRON_POST_SWEEP` posts Top/Daily Picks to the single configured
  group (`TELEGRAM_CHAT_ID`) plus the owner/admin — explicitly, by its
  own doc comment, never calling `sweepDeliveries()`.
- `CRON_RECAP_SWEEP` posts the group recap only.
- `sweepDeliveries()` — the one function in `workers/src/cron.ts` that
  broadcasts to individual subscriber chat IDs — is defined but is
  **never called from `handleScheduled()`, for any of the 4 scheduled
  crons.** This matches production's own documented model
  (`SUBSCRIBER_DELIVERY_ENABLED=false`, mirroring
  `predbot/jobs/daily_picks.py`'s group-only send).

So: **subscribers are not receiving any proactive broadcasts in
production today.** Nothing in this migration changes that — the
server-side crons call the exact same unmodified `handleScheduled()`.
A regression test (`workers/bot/tests/rehearsal-guard.test.mjs`,
"subscriber delivery is structurally disabled") asserts
`sweepDeliveries(` never appears in `handleScheduled`'s body, so this
stays caught if it's ever reintroduced. Because the function is
unreachable, the rehearsal guard's chat-ID allow-list has nothing to
gate here — it exists for the proactive sends that
*do* run (group/owner/admin posts), not for subscriber broadcasting.

### 3d. Not D1 at all (confirmed, not touched)

- `predictions-bot/` Python engine's own working data (fixtures, odds,
  predictions) lives in its own scratch SQLite files during a run, never
  touches D1 directly — only `d1_sync.py` (invoked from the workflow
  YAML, not from Python) ever talks to D1. This plan does not change
  anything inside `predictions-bot/`'s own code.
- `.batch()` is not called anywhere in `workers/src/` today (confirmed by
  search). `batch()` is still implemented in the new adapter because it's
  now the permanent interface, not a bot-only shim, and the real D1
  binding's `batch()` is atomic — a future caller should get real
  transactional semantics, not a missing method.

---

## 4. Architecture on the server

```
/opt/sureodds/                      git working tree, sparse-checked-out
  workers/
    src/*.ts                        UNCHANGED - same files as the repo
    bot/
      bot.ts                        long-polling entry point (from the old branch, adapter swapped)
      local-sqlite-adapter.ts       NEW - replaces d1-rest-adapter.ts
      cron/
        post-sweep.ts               calls handleScheduled(env, CRON_POST_SWEEP, ...)
        recap-sweep.ts              calls handleScheduled(env, CRON_RECAP_SWEEP, ...)
        deliver-sweep.ts            calls handleScheduled(env, CRON_DELIVER_SWEEP, ...)
        watch.ts                    calls handleScheduled(env, CRON_WATCH, ...)
      ssh-relay/
        receive-batch.sh            the forced-command target (see 6)
      *.service, *.timer            systemd units (bot + 4 crons)
    scripts/
      d1_sync.py                    UNCHANGED - the `apply` command already does what's needed
    migrations/*.sql                UNCHANGED - already plain SQLite
  predictions-bot/
    predbot/pro_value/
      closing.service, .timer       NEW systemd units (Python, unchanged script)

/var/lib/sureodds/
  sureodds.db                       the live SQLite database (WAL mode)
  sureodds.db-wal, sureodds.db-shm  WAL side files
  polling.offset                    bot's Telegram offset (unchanged mechanism from the old branch)
  incoming/                         batch directories arriving over SSH, before apply
  backups/                          local staging area before upload to Object Storage
```

---

## 5. Locking: no overlap between crons, or between a cron and the bot

Two distinct overlap problems, two distinct mechanisms:

**A. The same cron firing twice before the first run finished.** Each cron
becomes its own systemd `.service` + `.timer` pair (not one shared
service). A `.timer`-triggered `.service` that is still running when the
timer next elapses does not get a second concurrent instance — systemd
does not start a second instance of a unit that is already active. As
defense in depth (not relying solely on that default), each service's
`ExecStart` is also wrapped in `flock -w 60 /var/lock/sureodds-<job>.lock
-c '...'`: a 60-second wait for its own lock, then a loud failure
(visible in `journalctl`) rather than a silent skip or an indefinite hang.

**B. Two different jobs (e.g. post-sweep and the bot) writing to the
SQLite file at the same moment.** This is handled at the database layer,
not with an external lock: every connection (`LocalSqliteAdapter`, used by
the bot and every cron script identically) opens with:
```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```
WAL mode lets readers proceed without blocking on a writer; a second
writer that arrives while one transaction is in progress waits up to the
busy timeout rather than failing immediately, and SQLite's own
transaction manager guarantees no interleaved corruption either way. A
write that still hits `SQLITE_BUSY` after the timeout surfaces as a
distinct, named error (not swallowed) so it is visible in logs rather
than silently lost — this is the one new piece of error-handling logic
`local-sqlite-adapter.ts` needs beyond what the test mock already does.

Given the actual write volume (bot: per-message, usually several seconds
apart; sweeps: every 5 minutes, each a handful of statements; closing:
every 30 minutes), sustained contention past a 5-second busy timeout is
not expected — this is a safety margin, not a load-bearing assumption
under heavy load.

---

## 6. GitHub Actions → server delivery (SSH, forced command, restricted key)

**Key:** a dedicated SSH keypair, generated by GitHub Actions' own secret
storage (not reused from your own access key or mine). The **public** key
is installed on the server in `/home/sureodds/.ssh/authorized_keys` with a
forced command and restricted options:
```
command="/opt/sureodds/workers/bot/ssh-relay/receive-batch.sh",no-pty,no-X11-forwarding,no-agent-forwarding,no-port-forwarding ssh-ed25519 AAAA... github-actions-delivery
```
`command=` means this key can **only** ever run `receive-batch.sh`,
regardless of what command the connecting side asks for — even if the
private key leaked, it cannot open a shell, read arbitrary files, or run
anything else on the server.

**`receive-batch.sh`** (new, small): reads a tarred batch directory from
stdin (via `scp`'s own protocol, which the forced command can still
service since `scp` tunnels over the same SSH session), writes it to
`/var/lib/sureodds/incoming/<job>-<timestamp>/`, verifies a checksum
manifest the workflow includes (reusing the existing plan/push output,
which already produces a manifest — see `cmd_manifest`), then runs:
```bash
python3 /opt/sureodds/workers/scripts/d1_sync.py apply \
  /var/lib/sureodds/sureodds.db /var/lib/sureodds/incoming/<job>-<timestamp> \
  --expect-epoch "$EXPECT_EPOCH"
```
exactly the existing `apply` command, unmodified. The existing
plan/budget/inserts-only checks each workflow already runs (write-budget
caps, destructive-budget caps, `require-complete` gates) stay exactly as
they are today — they run on the Actions runner, before anything is sent,
same as now. Nothing about those checks changes; only where the resulting
batch lands changes.

**Workflow-side change**, per production-writing workflow (stage5, stage9,
stage10, stage11): replace the `rest-apply` step with a `scp`/`ssh` step
using the dedicated Actions-side key (stored as a GitHub secret, scoped to
this repo only) targeting the server's public IP, then check the script's
exit code the same way `rest-apply`'s exit code is checked today.

---

## 7. Backups: consistent SQLite snapshots to Oracle Object Storage

Not a raw file copy (WAL mode means `sureodds.db` alone, copied while the
process is live, is not a consistent snapshot — the `-wal` file holds
uncommitted-to-main-file changes). Instead:
```sql
VACUUM INTO '/var/lib/sureodds/backups/sureodds-<timestamp>.db';
```
SQLite's own `VACUUM INTO` produces a complete, consistent, single-file
snapshot as of that instant, safe to copy anywhere afterward, with no
special handling needed for the WAL side files. A systemd timer runs this
on a schedule (daily, proposed — open to adjustment), then uploads the
resulting file to Oracle Object Storage via `oci os object put` (Oracle's
CLI, installed once) using a bucket-scoped API key (not the account's main
credentials), then deletes local backups older than N days to keep the
1 GB disk (actually the VM's attached block volume, separately sized from
RAM) from filling up.

**Tested restore** (part of the rehearsal, §9): download a snapshot,
`sqlite3 restored.db .dump | sqlite3 verify.db` or a direct file copy plus
`PRAGMA integrity_check`, and confirm row counts per table match the
backup's own manifest (taken at backup time via `SELECT COUNT(*) FROM
<table>` for every table, stored alongside the snapshot).

---

## 8. Server setup

- **Node 24 LTS**, installed via `nvm` as the `sureodds` user (no sudo
  needed for Node itself — same approach as the earlier SSH runbook).
- **`sureodds` user**: no sudo, ever. Created once by root (the one
  unavoidable root step, same as before). All services — bot, 4 cron
  timers, the closing timer, the SSH relay, the backup timer — run as this
  user.
- **Deploy from the repo** at a tagged commit via sparse checkout (as
  established on the old branch) — not copied files. `/opt/sureodds`
  mirrors the real repo path.
- **Secrets**: a single `.env` file, mode 600, created by you over SSH
  with `nano` — same handling as the earlier runbook. Never pasted into
  chat, never committed.
- **systemd**: every service unit gets `Restart=always` and `MemoryMax`
  (see §10 for the actual numbers); a 2 GB swap file (`fallocate` +
  `mkswap` + `swapon`, added to `/etc/fstab`) as a safety margin, not a
  load-bearing assumption.

### Your own SSH access (separate from GitHub Actions' key)

Same procedure as given earlier in this conversation, repeated here for
this plan's record:
```bash
# On your machine:
ssh-keygen -t ed25519 -f ~/.ssh/sureodds_oracle -C "sureodds-deployment-$(date +%Y%m%d)" -N ""
```
Send me the **public** key file's contents (`~/.ssh/sureodds_oracle.pub`)
— I'll give you the exact `authorized_keys` line to add for the
`sureodds` user (unrestricted, since this is your own administrative
access, unlike the Actions key above). You keep the private key; I never
see it.

---

## 9. Memory budget for 1 GB RAM

Steady-state (what's resident continuously, not transient cron spikes):

| Process | Estimate |
|---|---|
| Ubuntu 26.04 Minimal + systemd baseline | ~150–250 MB |
| Bot (`bot.ts`, Node 24, long-polling, holds the SQLite connection open) | ~80–150 MB |
| SQLite page cache (shared via WAL, bounded by `PRAGMA cache_size`) | ~20–50 MB |
| **Steady-state total** | **~250–450 MB** |

Transient (only while a 5-minute or 30-minute timer is actually running,
typically a few seconds):

| Process | Estimate |
|---|---|
| A single cron script (post/recap/deliver/watch) | ~60–100 MB each, one at a time (locking, §5, prevents concurrent cron processes) |
| The closing job (Python) | ~80–150 MB, every 30 min, brief |

Worst case overlap (bot + one cron + the closing job, all resident at
once, which locking makes rare but not impossible for a few seconds):
roughly 250–450 MB (steady) + 100 MB (one cron) + 150 MB (closing) ≈
**500–700 MB**, comfortably under 1 GB with the 2 GB swap as a margin for
any spike beyond this estimate. This fits, but with less headroom than the
Stage 1 bot-only design had — the estimate alone is not trusted.

Correction: an earlier version of this doc described the busiest overlap
as including a "subscriber delivery sweep." That was wrong — read
`handleScheduled` in `workers/src/cron.ts`: `CRON_DELIVER_SWEEP` calls
`reportFailedUpdates`, `sweepMatchEvents` (group + owner only),
and the three `maybeDispatch*` GitHub Actions triggers. It never calls
`sweepDeliveries()` — the one function that broadcasts to individual
subscriber chat IDs — for any of the 4 scheduled crons. Production
has no subscriber-broadcast path today; see §3c above for the fuller
explanation.

### Peak memory: a pass/fail rehearsal item, not just an estimate

During the rehearsal (stop point 4), the busiest realistic overlap —
the deliver-sweep cron (match-event delivery + Actions dispatch checks)
+ a settlement `apply` running + normal bot traffic, all at the same
time — must be deliberately produced (not just waited for), and peak RSS
measured across every resident process with:
```bash
systemd-cgtop -m -1 -b -n 1 | grep sureodds   # per-unit memory, one sample
# or, for a precise peak over the whole window:
while true; do date -u +%s; ps -eo pid,comm,rss | grep -E "node|python3|sureodds"; sleep 5; done >> /tmp/mem-watch.log
```

**Pass:** peak total resident memory (bot + whichever cron/closing/apply
processes are briefly overlapping) stays at or under **800 MB**, leaving
at least 200 MB of the 1 GB for the OS/systemd baseline without touching
swap under normal load.
**Fail:** peak exceeds 800 MB, or the OOM killer fires, or swap is used
non-trivially (more than a brief, small spike) during the window.

On fail: the stated fallback (stop and report, not silently worked
around) is either a different Always-Free-eligible shape with more RAM,
a small paid upgrade, or trimming `PRAGMA cache_size` and staggering the
cron schedule further apart - decided with you, not unilaterally.

**If the rehearsal shows this doesn't fit**, the stated fallback (per your
rule: stop and tell you) is to either raise the Oracle VM's shape (a
different Always-Free-eligible shape, if one exists with more RAM, or a
small paid upgrade) or trim `PRAGMA cache_size` and stagger the cron
schedule further apart — I will not silently work around a real misfit.

---

## 10. Effort estimate

| Phase | Work | Estimate |
|---|---|---|
| Build: local SQLite adapter + `batch()` + WAL/busy-timeout | `local-sqlite-adapter.ts`, migrated from the old branch's REST adapter shape | 1 day |
| Build: bot.ts adapted (swap adapter import only) | Minimal - the old branch's bot.ts already isolates the adapter behind the `Env.DB` interface | 0.5 day |
| Build: 4 cron entry scripts + systemd units + locking | New small files, each a thin wrapper around `handleScheduled` | 1.5 days |
| Build: closing job systemd unit (Python, unchanged script) | Just the unit file + env wiring | 0.5 day |
| Build: SSH relay (forced command, receive-batch.sh) + workflow changes (4 workflows) | New shell script + GitHub Actions YAML edits, reusing existing `plan`/`apply` | 2 days |
| Build: backup/restore (VACUUM INTO, Object Storage upload, restore + verify) | New systemd timer + script | 1 day |
| Tests: adapter contract + compat tests (mirroring the old branch's approach) | Reuse the established pattern | 1 day |
| Server setup: Node, user, sparse checkout, secrets, SSH keys | Mostly your side (runbook), some coordination | 0.5 day |
| **Rehearsal** (stop point 3→4): full exercise listed in your message | Real time, not just my time - includes multi-hour soak and a deliberate crash/reboot | 1–2 days elapsed (my prep: ~1 day) |
| Cutover plan write-up (this document's §11, expanded before stop point 5) | Already drafted below | included above |
| **Total engineering** | | **~9–10 days** |
| **Total elapsed**, including your rehearsal time and the freeze window itself | | **~2–3 weeks**, most of it waiting on your testing windows, not continuous work |

This is larger than the original bot-only Stage 1 (~7 days) because it now
includes the database, all 4 crons, the closing job, the Actions delivery
mechanism, and backups — the full system, as asked.

---

## 11. Cutover plan (written now, not executed)

### Freeze
1. Disable the Cloudflare Cron Triggers (comment out `[triggers]` in
   `wrangler.toml`, redeploy the Worker) — stops the 4 sweeps from writing.
2. Disable the GitHub Actions schedules that write to D1 (generation,
   settlement, history backfill, closing-price backfill) — either by
   removing their `schedule:` trigger temporarily or via GitHub's own
   "disable workflow" button (reversible, no file edit needed).
3. Confirm via `wrangler tail` and the Actions run history that nothing
   new is writing. **Freeze duration: the time it takes to do steps 1–2
   and confirm step 3, plus the export/import below — estimated 30–60
   minutes total**, not an extended outage. The bot itself can stay on
   the webhook throughout the freeze; only the cutover moment (below)
   switches it.

### Export, import, verify
4. Export production D1 to a local SQLite file (`d1_sync.py` already has
   the tooling to pull a full snapshot - `cmd_snapshot` or equivalent).
5. Copy that file to the server as `/var/lib/sureodds/sureodds.db`.
6. Verify: row counts per table, and a checksum (e.g. `sha256sum` on a
   `.dump` output, or per-table hash via `cmd_manifest`'s own fingerprint
   logic) compared between the D1 export and the server's copy. Any
   mismatch stops the cutover here — does not proceed to the next step.

### Switch
7. `deleteWebhook` on the live bot token (Telegram keeps any updates that
   arrive between this and step 8 — nothing is dropped, per Telegram's own
   `getUpdates` semantics, same guarantee relied on in the earlier
   staging runbook).
8. Start the bot service and the 4 cron timers and the closing timer on
   the server.
9. Point the 4 production-writing GitHub Actions workflows at the
   server's SSH relay (the workflow YAML change from §6 — deployed ahead
   of time, but only *exercised* for real from this point on; nothing
   stops it from being dormant/unused during the freeze).
10. Re-enable the Actions schedules and the generation/settlement
    pipeline resumes its normal cadence.

### First-run checks
11. Watch the first post sweep, recap sweep, and delivery sweep on the
    server (`journalctl -u sureodds-post-sweep -f` etc.) — confirm the
    group/owner messages land exactly as they do today.
12. Watch the first generation delivery and the first settlement delivery
    over SSH — confirm `apply` reports the expected batch count, and spot
    check a few rows against what generation/settlement actually produced.
13. Watch the first closing-capture run on its new 30-minute timer.

### Rollback
If anything in steps 7–13 goes wrong:
1. Stop the server's bot service and all timers.
2. Re-set the Telegram webhook to the Worker's existing endpoint (the
   Worker, its webhook secret, and D1 are **kept intact and untouched for
   2 weeks after cutover**, specifically so this is always a live option,
   not a restore-from-backup exercise).
3. Copy back anything written to the server's SQLite **since cutover**
   (new subscribers, new custom-picks sessions, new issued tickets, new
   settlement rows) into D1 — a reverse `d1_sync.py plan` (treating the
   server's SQLite as the new source, D1's last-known state as the base)
   produces exactly the diff needed; `rest-apply` pushes it back to D1,
   the same tooling used in the forward direction.
4. Re-enable the Cloudflare Cron Triggers and the GitHub Actions
   schedules pointed back at D1's `rest-apply`.
5. Confirm via `wrangler tail` that the Worker is receiving webhook
   traffic again.

This rollback is symmetric with the cutover by design — the same
`plan`/`apply`/`rest-apply` tooling runs in either direction, which is
also why keeping D1 and the Worker alive for 2 weeks costs nothing extra
(they're not polling or writing anything on their own once the Cron
Triggers are off; D1 just sits there as a restore target).

---

## 12. Stop points (restated from your message, for tracking)

1. **This document** — written plan, effort estimates, D1 call-site
   inventory. ✅ Delivered here.
2. After the build, with test results.
3. Before the rehearsal: exact list of what you need to do (test bot,
   `.env`, keys) — a runbook, similar in form to the one already written
   for the bot-only plan, expanded to cover the database, crons, closing
   job, SSH relay key, and backup/restore.
4. After the rehearsal, with results against explicit pass/fail criteria
   (to be listed in the stop-point-3 runbook, covering every item in your
   "Exercise" list: bot commands, Custom Picks, Pro Picks, posting, recap,
   delivery, watch, closing capture, a generation delivery, a settlement
   delivery, a backup and restore, a crash, and a reboot).
5. Before cutover — you approve the date and time; nothing here starts a
   clock on its own.

Nothing beyond this document has been built or executed. Waiting for your
go-ahead to proceed to the build (stop point 2).
