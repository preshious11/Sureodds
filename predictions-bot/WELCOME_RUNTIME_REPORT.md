# Welcome/runtime investigation — 2026-09-16

## Finding and limits

The reported paragraph is historical code, not a fallback in the current tree.
Commit `02e9153` introduced it in `predbot/bot/callbacks.py` as `WELCOME`.
Commit `5ce69a9` removed it on September 15 at 23:25 +0100, replacing both
the `/start` response and menu/subscription callback response. Current HEAD is
`80c1086` on branch `main`; even HEAD already has the personalized greeting.
There were 11 modified files before this investigation. Those changes were
preserved; a hosted Git deployment does not receive uncommitted local edits.

An exhaustive byte search of repository files (including ignored files, logs,
cache and compiled artifacts, excluding Git internals) found no occurrence of
any of the six supplied old-copy strings before this change. Historical Git
objects retain them intentionally. New negative tests reference three strings
to guard against regression. There is no welcome override in config, database
schema, cache code, templates or environment-loading code.

Consequently a *new response* containing that exact old paragraph indicates
older loaded code or another deployment/project. Editing a Python file does not
replace modules already loaded into a running process. This isolates the
reversion to runtime/deployment provenance; it does not establish whether the
specific culprit is an old replica, an old in-memory process, or an old commit
being restarted. No production service inventory or production logs were
available, so claiming the precise minutes-later trigger was proven would be
incorrect. Production resolution remains pending.

## Runtime evidence

- Windows process inspection found no Python, Railway or Docker process at the
  time of inspection. No Railway or Docker CLI was installed.
- Read-only Telegram `getMe` identified `@BETTSUREbot`; `getWebhookInfo` returned
  an empty webhook URL and zero pending updates. These calls did not consume
  updates or send messages. Telegram does not expose a list of polling clients.
- Local `bot.out.log` records command loop and scheduler startup at
  2026-09-15 14:17:35, before the welcome-removal commit. It records repeated DNS
  failures around 15:49:15 and later scheduler activity. This demonstrates an
  older local run, not that it is still alive or that it caused every reversion.
- `bot.err.log` is empty. No recorded restart proves the reported trigger.
- Previously every non-200 getUpdates response, including 409 conflicts, was
  silently discarded. DNS errors returned immediately, causing rapid retries.
  Those logs also contained credential-bearing URLs; do not share raw logs.
- No hot reload, checkout/reset at startup, delayed handler registration,
  subprocess bot spawn, webhook consumer or second polling startup exists in
  the repository. Jobs refresh prediction data, not welcome text or handlers.

## Active code path and deployment entry points

`main.py -> CommandLoop.start -> CommandLoop.run -> Telegram.get_updates`

`Telegram /start -> CommandLoop._poll -> callbacks.handle_start ->
callbacks.welcome_message + callbacks.menu_markup -> Telegram.send -> sendMessage`

There is one static `/start` branch and one callback router, not a framework
handler registry. Reinitializing creates the same branch, not another registered
handler. Legacy `menu`, `sub|on`, `sub|off` reuse the canonical formatter.

The root Dockerfile copies `predictions-bot/` into `/app/predictions-bot` and
executes `python main.py`. The nested Procfile and railway.json also execute
`python main.py`, but require `predictions-bot/` as the working/project directory.
The nested Railway config specifies Nixpacks and on-failure retries (10).
The active hosted build mode/root/start command cannot be inferred from these
files alone; verify the service settings and deployment logs.

`cli.py` constructs a Telegram client for manual outbound jobs; it never consumes
updates. The other client construction is in `main.py`. No additional application
builders or consumer entry points were found. Scheduler job timing and selection
logic were not changed.

## Files changed by this task

- `predbot/bot/callbacks.py`: exact concise welcome, escaped Telegram first name,
  four welcome buttons, restored subscription and legacy-menu callback handling.
  Existing shared `open_view` remains the route for commands and view buttons.
- `predbot/bot/commands.py`: local polling ownership, startup/response identity,
  safe failure logging and 30-second polling-error backoff; menu controls work
  without a prediction service. Rename the stop event to avoid shadowing
  Thread._stop, which broke thread joining.
- `predbot/bot/telegram.py`: explicit sanitized HTTP/transport polling errors,
  including actionable 409 conflict diagnostics.
- `predbot/bot/runtime.py` (new): OS-held local token-scoped polling lock; startup
  timestamp, hostname, PID, commit, source hash, service/deployment identity and
  active handler source. The lock is local only; separate containers/machines
  still require one configured consumer. Old code does not honor this lock.
- `main.py`: retain and stop/join the command thread; fail if it dies instead of
  silently running a scheduler-only process.
- `predbot/jobs/scheduler.py`: optional health check before each scheduling pass.
  Checks run between jobs; they do not interrupt a currently executing job.
- `tests/test_subscribers_broadcast.py`: update only the welcome-menu expectation.
- `tests/test_welcome_runtime.py` (new): state/repeat/restart, fresh-process,
  escaping, all three shared routes, unsubscribe, lock, error/backoff and health tests.
- `WELCOME_RUNTIME_REPORT.md` (new): findings and production verification.

Other files shown modified by Git predate this task, including prediction UI,
broadcast, selection-related configuration and cache changes. They were not
reverted or changed here.

## Final welcome and callbacks

```text
👋 Hi, Ada

Tap the menu below or use a command:

/top — Strongest picks today
/daily — All predictions today
/stats — Prediction summary
```

`Ada` is the incoming Telegram user's first_name (HTML-escaped for transport).

| Button | Callback | Shared behavior |
| --- | --- | --- |
| 🏆 Top Picks | pg\|top\|0 | /top -> open_view(top) |
| 📋 Daily Picks | pg\|daily\|0 | /daily -> open_view(daily) |
| 📊 Stats | pg\|stats\|0 | /stats -> open_view(stats) |
| 🔕 Unsubscribe from daily picks | sub\|off | subscriber_remove |

After unsubscribe, the fourth button becomes Subscribe (`sub|on`). The welcome
text stays identical. `/start` retains the existing re-subscription behavior;
`menu` alone does not re-subscribe. `/stop` still unsubscribes. View buttons edit
the existing message while slash commands send a new message, with identical
rendered content and keyboard.

## Tests

Before changes: 134 passed, 6 failed. Focused final suite: 33 passed.
Final full suite: 152 passed, the same 6 failed; no new regression failures.
The unchanged baseline failures are:

- test_accumulator.py::test_total_counts_are_reported_per_section
- test_cache_pagination.py::test_render_page_structure_and_keyboard
- test_cache_pagination.py::test_callback_navigation_is_zero_api
- test_picks_format.py::test_card_matches_the_requested_layout
- test_picks_format.py::test_top_picks_are_listed_first_then_the_daily_ones
- test_picks_format.py::test_every_pick_gets_the_six_line_block

These expect different prediction-card wording/layout. They were not edited.

## Required deployment action and exact verification

1. Review and commit the intended working changes, then push/deploy that commit
   to the production service. Nothing was committed, pushed, stopped or deployed
   by this investigation. Record `git rev-parse HEAD` after committing.
2. In Railway, inspect every project/service/environment using this bot token.
   Keep exactly one intended polling worker replica. Check active deployment SHA
   and branch against the recorded commit; stop confirmed obsolete consumers.
   Check other computers/hosts for local copies as well. Do not merely redeploy
   one service while leaving an older service active.
3. Verify entry point: root Dockerfile deployment uses its existing WORKDIR/CMD;
   a nested Nixpacks deployment must use root directory `predictions-bot` and
   start command `python main.py`. A command started from repository root must
   first change into `predictions-bot`. Do not run a local production-token bot
   while hosted polling is enabled.
4. In fresh production logs locate `Bot instance started`. Verify `mode=polling`,
   expected commit, service/deployment and
   `start_handler=predbot.bot.callbacks.handle_start`. Compare the source hash
   in `instance` with this command run from the nested project directory:

   ```powershell
   python -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('predbot/bot/callbacks.py').read_bytes()).hexdigest()[:16])"
   ```

   Compare hashes on the deployed checkout if Git converts line endings. A Git
   SHA alone does not identify uncommitted edits; the hash identifies source bytes.
5. Send `/start` to `@BETTSUREbot` three times. Confirm the exact welcome and all
   four buttons. Each request must have a matching `start handled` log with the
   expected instance. Missing logs alongside an old response implicate another
   runtime (also confirm you are viewing the correct service's logs).
6. Compare `/top`, `/daily`, `/stats` against their buttons. Unsubscribe, open an
   older menu button, re-subscribe, `/stop`, then `/start`. The greeting must never
   change into subscription prose; active state should change appropriately.
7. Repeat `/start` after 5, 10 and 20 minutes and after a normal cache-refresh/job
   cycle. Watch for HTTP 409 conflicts, startup restarts or changed instance IDs.
   Do not force a prediction broadcast merely to test the menu.
8. Restart the intended hosted deployment once. Confirm a new PID/startup time,
   unchanged expected source hash/commit, and repeat steps 5–7. This production
   observation is required before declaring the recurring issue resolved.

Reproduce repository tests from `predictions-bot/`:

```powershell
python -m pytest -q tests/test_welcome_runtime.py tests/test_subscribers_broadcast.py -p no:cacheprovider
python -m pytest -q -p no:cacheprovider --tb=no
```

No credentials were rotated and no service was deleted by this investigation.
The obsolete consumer was later identified and deleted; see *Resolution* below.
A code fix cannot evict an unknown old hosted consumer; the ownership/deployment
checks above remain necessary even with the new local lock and diagnostics.

---

## Resolution (2026-09-16)

The recurring conflict was confirmed in production and closed the same day.

**Root cause.** A **Railway** worker service (`predictions-bot/railway.json` +
`Procfile: worker: python main.py`) was still deployed and polling with the same
`TELEGRAM_BOT_TOKEN` while justrunmy.app ran the root-`Dockerfile` image. Both
consumed `getUpdates` on `@BETTSUREbot`, so the hosted deployment lost ownership
of the token. The Railway worker had survived a push and was never stopped.

**Production evidence.**

- justrunmy.app logs showed `Telegram polling conflict (409)` at a **60.2 s beat
  for 91+ minutes** (2026-09-16T13:58Z -> 15:29Z) and never won a cycle.
- The beat matches this code exactly: the in-flight long-poll in
  `predbot/bot/telegram.py` (`timeout=20`, request timeout `+10`) terminated with
  409, plus the 30 s back-off for `PollingError` in `CommandLoop._poll()`
  (`predbot/bot/commands.py`). A losing duplicate never exits and never recovers.
- `getWebhookInfo` returned `url=''` (the competitor was a poller, not a webhook)
  and `pending_update_count=0`, i.e. the competitor was draining the queue, so
  user commands were being served outside the intended deployment.
- Scheduled work was unaffected on the hosted side: `deliver` every 5 minutes,
  the daily job and the snapshot write all succeeded during the conflict.

**Local host ruled out.** Full `Win32_Process` command-line scans taken *during*
the conflict window found no Python/predbot process; no Docker, WSL, scheduled
task, service, Run key or Startup entry could launch one either. The
`%TEMP%\predbot-poll-*.lock` files were inert (OS-held locks released on exit).

**Diagnostic caveat.** `getWebhookInfo` does **not** record `getUpdates`
conflicts (`last_error_message` stays `None`), so it cannot rule out a duplicate
poller. Only production logs (409 lines) or a deliberate `getUpdates` call reveal
this fault, and the latter risks stealing updates from the healthy instance.

**Fix applied.** The service owner deleted the Railway service. No token was
rotated and no code was changed.

**Verification after the deletion** (hosted logs):

1. Zero new `Telegram polling conflict (409)` lines within two minutes.
2. Exactly one `Bot instance started` line per deployment, with
   `service=local`/`deployment=local` for the container host.
3. A `/start` sent from Telegram produces `start handled | instance=...`.

**Standing rule.** Exactly one polling consumer per token. When moving hosts,
stop the previous service *before* starting the new one - never redeploy one host
while an older worker is still running.

**Residual risk.** `CommandLoop.__init__` starts with `offset = None`, so updates
that the deleted consumer fetched but never confirmed can be delivered once to the
surviving instance. Expect at most a small number of replayed commands right after
a cut-over; it self-clears on the next successful poll.
