# SureOdds Oracle production recovery — November 1, 2026

All human-facing times below use **Africa/Lagos (UTC+1)**. SQL and service evidence retains UTC where explicitly labeled. No credentials or destination chat identifiers are included.

**Status: substantial recovery is live, but final acceptance is not complete.** Oracle runs the dedicated recovery branch. Real start/end delivery and authoritative settlement have been verified. `main` has not been changed: automatic approval review rejected that operation pending explicit owner approval. Review: https://github.com/preshious11/Sureodds/pull/4.

## 1. Confirmed root causes and corrections

### A. Match-start and match-stop notifications

- **Original symptom:** neither notification arrived after migration.
- **Confirmed root causes:** Actions monitoring exported its input from the retired D1 application database while applying its output to Oracle. Oracle's newer predictions never entered monitoring. Independently, Oracle omitted `MATCH_START_WINDOW_MIN`; its sender defaulted to 10 minutes while the monitor used 45. A genuine start event was consequently expired before delivery.
- **Evidence:** successful pre-repair Actions runs coexisted with Oracle's newest fixture check at `2026-10-28T04:32:05Z`. After correcting the source, ESPN detected Como–Venezia in progress, but the sender recorded `kickoff window elapsed` at 12:45:11 Lagos, approximately 15 minutes after kickoff. The production environment inspection confirmed that the setting was absent.
- **Implemented correction:** server-target workflows now read a consistent Oracle snapshot; the apply fence uses that snapshot's epoch. Oracle's start window is 45 minutes, and the server environment builder supplies the same default. The erroneous expiry was preserved under an audit receipt name; no successful delivery receipt was removed. Full settlement can also queue missed recent completion events after final evidence, grading, and actual publication are confirmed.
- **Tests performed:** existing Worker delivery tests exercised failed sends, independent destinations, repeated polling, final-state eligibility and receipts. New recovery tests cover final-event creation, real publication, duplicate prevention and exact result identity.
- **Production verification:** Como–Venezia start delivered at **12:47:52 Lagos**, confirmed by group message **440** and owner message **1661**. Thirteen recovered completion events subsequently produced **13 group and 13 owner receipts**. Example: Espanyol–Villarreal, group **441**, owner **1663**, at **13:15:07 Lagos**. A repeat delivery sweep succeeded without increasing the 184 historical end-delivery receipts. Ordinary subscriber automation was not restored.

### B. Pending predictions and tickets

- **Original symptom:** finished predictions and issued tickets stayed pending.
- **Confirmed root causes:** the same stale D1 input omitted Oracle predictions; full settlement selected only Top/Daily memberships, excluding All-only predictions; the issued-ticket grading helper explicitly stated that it was not wired into scheduling; elapsed deadlines permanently voided unresolved predictions and prevented subsequent result lookup. Two additional name mismatches prevented authoritative ESPN results from matching `Stade Brest` and `Athletic Bilbao`.
- **Evidence:** all 40 issued tickets initially remained `issued`. The database contained **193 voids whose sole recorded reason was `settlement deadline expired`**. ESPN's final scoreboards contained Brest–Nice, event `401876428`, and Athletic Club–Getafe, event `401882835`, while the original matching calls returned no result.
- **Implemented correction:** full settlement now grades all due predictions, retaining the existing market grader. Both full settlement and live monitoring update issued legs and parent ticket completion; live monitoring reuses stored evidence, while full settlement may retrieve results. Issued prices and contracts remain immutable. Missing final evidence stays pending and retryable. Only the 193 demonstrably unverified timeout voids were reopened; authoritative cancellations were preserved. Exact ESPN team-ID aliases are scoped to Ligue 1/team `6997` and La Liga/team `93`; fuzzy thresholds were not lowered.
- **Tests performed:** win/loss/draw/void and existing grader coverage; delayed and missing results; issued-price preservation; repeated settlement; wrong-ID/league/reserve-team rejection; monotonic relay updates; authoritative-only overdue grading. Recorded ESPN response facts supply the mapping regressions.
- **Production verification:** [run 41304854056](https://github.com/preshious11/Sureodds/actions/runs/41304854056) completed successfully and recorded **2,362 graded predictions, 17 authoritative voids, zero recorded grading errors, and 13 queued final events**. A subsequent targeted run of the two verified ESPN fixtures graded **10 more predictions** and **2 issued legs**, without errors. Issued tickets reached **37 settled / 3 unresolved**, with **3 pending legs**. No timeout-reason voids remained. The run's `pending` counter counts attempts in multiple passes; it is not the final number of unresolved database rows.

### C. Odds below the expected targets

- **Original symptom:** tickets at 1.44, 4.0 or other below-target totals were treated as normal.
- **Confirmed root cause:** the selectors explicitly permitted below-target tickets. Both selectors retained their best below-band fallback when no combination reached the target. Configuration comments explicitly documented publishing 6.4 when the Daily target could not be reached. The individual historical 1.44/4.0 messages were not independently reconstructed; no rounding or provider-repricing defect was established.
- **Evidence:** `select_top` and `select_daily` returned nonempty selections with `target_reached=False`; their search fallback and configuration documentation confirmed that contract.
- **Implemented correction:** Top requires actual combined odds within its configured band, default **2.0–2.5**; Daily requires **10.0–12.0**. An unreachable band returns no ticket with a diagnostic reason. Nonfinite, missing, boolean, zero and sub-decimal prices cannot enter ticket search. Individual price, probability, evidence, bookmaker, diversity and value gates were not relaxed. Documentation now describes withholding.
- **Tests performed:** exact target, below target, above ceiling, invalid prices and multiplication of distinct-fixture accumulator prices.
- **Production verification:** Oracle's repaired November 1 collections contain **2 Top legs at 2.0088** and **5 Daily legs at 10.38768956288232**, computed from persisted prices. These are the original generated contracts, recovered without repricing. A new post-repair generation/publication day has not yet occurred; that future scheduled lifecycle is not claimed verified.

### D. November 1 “insufficient picks” incident

- **Original symptom:** the bot reported insufficient Daily/Top picks despite many fixtures and an expected quota reset.
- **Confirmed root cause:** generation succeeded, but stale-source integer IDs collided with existing Oracle predictions. `INSERT OR IGNORE` silently discarded new rows; the day cache still contained the generated tickets. Views read authoritative prediction/membership rows and found no November 1 predictions. This was a persistence/identity failure, not an empty selection pool.
- **Evidence:** before repair, November 1 had a `COMPLETE` cache with 228 representative predictions and valid Top/Daily tickets, but no corresponding November 1 prediction rows. Cache IDs such as 2328 pointed at older Oracle records. Restoring exact retained batch contracts resolved those references.
- **Implemented correction:** source snapshots, source-epoch fencing, shared computation leases, lossless ID recovery, and a database trigger that rejects conflicting immutable prediction IDs instead of silently ignoring them.
- **Tests performed:** source snapshot preserves Oracle-only IDs, sequence and epoch; recovery preserves settled history, validates cache/batch agreement, and is idempotent; database guards reject conflicting IDs.
- **Production verification:** all **3,188 cached prediction references** checked across retained dates match their authoritative day, teams, market, selection and price. Current November 1 views can retrieve the recovered tickets. Existing empty publication receipts were retained, so the repair did not duplicate historical Daily/Top broadcasts.

## 2. Oracle deployment audit

The actual deployment is **Node long polling plus systemd sweeps, Oracle SQLite, and Python Actions computation**. It is not the long-running Python `main.py` scheduler. D1 still supplies coordination leases and legacy workflow paths; removing every Cloudflare reference would have broken existing coordination.

Verified runtime: Node **v24.22.1**; system and virtualenv Python **3.14.4**. Node resolves to `/home/sureodds/.nvm/versions/node/v24.22.1/bin/node`; the generic Python runtime link resolves to `/usr/bin/python3.14`. The Python virtualenv contains requests 2.34.2, Pillow 13.0.0 and qrcode 8.2. Python application computation normally executes in Actions with workflow-provided dependencies and provider secrets.

`sureodds-bot.service` runs as `sureodds`, working directory `/opt/sureodds/workers/bot`, with an explicit environment file and absolute database path. The restarted process is PID **98266**, active/running, with no automatic restarts recorded. Restart policy is `always`; the unit now waits for network-online and has no start-limit cutoff. The manual restart and continued polling were verified; an unexpected production crash was not deliberately injected.

The UTC host clock is NTP synchronized. Application timezone is Africa/Lagos. No clock/date-boundary defect was established. SQLite uses WAL, integrity checks pass, and roughly **42.6 GB** remained free. No database-lock or disk-capacity failure was established.

Provider keys are absent from the Oracle Node environment because Python provider work runs in Actions. This absence was not incorrectly treated as proof of a broken Node service. The disabled local Pro closing timer was left disabled; its equivalent Actions workflow is active. Enabling that timer would require provider credentials and correct virtualenv routing. No public Pro publishing mode or publishing gate was enabled.

## 3. Notification repair and delivery policy

The repaired chain is Oracle predictions → Actions snapshot → provider status → durable `match_events` → Oracle apply → sender eligibility → independent group/owner claims → Telegram confirmation → message-ID receipt.

Completion events require authoritative final evidence, settled published legs and actual publication receipts. Historical recovery queues only recent fixtures, using the established formatter and delivery sweep. Ticket-ledger settlement does not introduce unsolicited private notifications to ordinary users.

Real Telegram confirmation, not process uptime or code edits, establishes the delivery evidence in section 1. The standalone health check never sends a Telegram message.

## 4. Settlement repair and remaining pending data

At the 13:30 Lagos database snapshot, prediction statuses were: **1,531 won; 876 lost; 76 push; 16 half-won; 11 half-lost; 19 void; 553 pending**. These include historical and All-only predictions; they are not the published-ticket win-rate denominator.

The health snapshot identified **323 predictions overdue by more than 12 hours**. No authoritative final was present on another leg of those unresolved fixtures at the audit checkpoint. They were not force-settled. Their unresolved full-match markets were result2/result3, total, handicap, double chance, DNB and BTTS, rather than a backlog dominated by missing half-time statistics.

Coverage gaps include records labeled Euroleague that are actually women's fixtures and tennis fixtures outside the available scoreboard coverage. Additional provider naming gaps may remain; they have not all been individually proven or corrected. This is an explicit remaining investigation, not evidence that every remaining match is still in progress.

The three unresolved issued legs are one historical Maria Fernanda Herazo Gonzalez–Victoria Rodriguez match without a verified result, and two November 1 MLS half-time selections whose fixtures were still in the future at verification. No scores were invented to close them.

## 5. Odds and model evidence audit

Targets are **combined ticket odds**, not minimum odds per individual selection. Default configuration remains `TOP_COMBINED_ODDS=2`, `TOP_ODDS_MAX=2.5`, `DAILY_COMBINED_MIN_ODDS=10`, `DAILY_COMBINED_MAX_ODDS=12`.

The generation snapshot records ESPN insight attempted for 68 fixtures, with data returned for 42 and none for 26. Among 230 current-day stored rows, including promoted alternatives, **82 carry an independent-evidence flag and 148 do not**. They must not all be described as independently validated. Existing provenance was preserved; no model weights, confidence floors or evidence thresholds were changed. This incident repair does not establish predictive profitability or independently revalidate every model's calibration.

## 6. November 1 pipeline and quota evidence

| Stage | Recorded result |
|---|---:|
| Discovered fixtures after the engine's discovery/merging boundary | 68 |
| Fixtures with valid candidates | 68 |
| Fixtures represented in All Predictions | 67 |
| No representative selection | 1 |
| Representative predictions | 228 |
| Archived ticket candidates | 2,090 |
| Top candidates passing promotion gates | 26 |
| Top selected legs | 2 |
| Daily input after excluding Top's two exact selections | 2,088 |
| Daily candidates passing promotion gates | 76 |
| Daily selected legs | 5 |
| November 1 authoritative prediction rows before ID recovery | 0 |

Top promotion rejections: probability 1,778; disabled market 108; insufficient books 173; disagreement 5. These plus 26 eligible candidates equal 2,090. Daily promotion rejections: probability 1,534; disabled market 108; insufficient books 310; poor model value 32; price range 13; disagreement 15. These plus 76 eligible candidates equal 2,088. Construction additionally recorded duplicate-event and diversity decisions. Raw provider responses overlap and are not an additional unique-fixture count.

The restored rejection log includes both aggregate summary rows and sampled examples. Summing every row's `count` would double-count sampled examples; the generation's aggregate rejection counters above are the correct funnel totals.

An Oracle-origin, authenticated Odds API sports probe returned **HTTP 200, 78 sports, 491 requests remaining, 9 used, and zero credits for that probe**. It used the existing local provider credential transiently over SSH, without storing or printing it. This proves that credential's current authentication/quota from Oracle; it does not reveal the Actions secret value or prove the account's exact billing reset timestamp. No reset header was supplied. The generation snapshot independently records 9 Odds API credits used. Quota exhaustion is not supported as the cause of this incident.

## 7. Database consistency and safe corrections

- Verified private backup before repair: `/var/lib/sureodds/recovery-20261101/before.db`, **90,677,248 bytes**, SQLite integrity `ok`, SHA-256 `add90cddfa750a61b13a545e9e2188698c51f3e42f963fe53f419889d10c1ef2`.
- Additional verified checkpoints: `before-authoritative-settlement.db` and `after-bulk-settlement.db`. Service and secret environment backups remain private in the same recovery directory.
- Recovered **414** exact cache-referenced prediction contracts; remapped **424** cache references; added **424** memberships; removed **4** provably incorrect collection links. Existing prediction prices, results and publication receipts were preserved.
- Recovered **986** rejection diagnostics without reusing conflicting integer IDs. A rollback-only rehearsal showed zero further prediction/cache/membership changes before applying that diagnostic recovery.
- Reopened only the **193** timeout-reason voids, preserving their original evidence in a private audit file. Two pre-existing authoritative cancellation/postponement voids were not reopened.
- Applied `0017_oracle_recovery_guards.sql`: additive triggers reject stale immutable-ID collisions and unverified timeout voids. No production database was deleted or recreated.
- Final checks: **0 foreign-key violations, 0 orphan memberships, 0 mismatched cache references; integrity `ok`**.
- Observed table counts: picks 3,082; memberships 3,215; ticket candidates 6,976; match results 1,501; live fixture states 101; selection rejections 1,900. Counts can advance as live processing continues.

## 8. Modified files

Only recovery files were committed. The owner's three pre-existing deleted documents and unrelated untracked work were preserved.

| File | Purpose |
|---|---|
| `.github/workflows/stage5-acceptance-production.yml` | Oracle source snapshot, source epoch, early shared lease, preserved exit status, usage/job metadata sync |
| `.github/workflows/stage8-live-monitor-production.yml` | Oracle source/epoch, issued-ledger and job-state sync, loud apply failure |
| `.github/workflows/stage9-settle-production.yml` | Oracle source/epoch, early lease, all settlement/event/ledger tables, bounded backlog write budget |
| `.github/workflows/stage10-history-backfill-production.yml` | Oracle source and source-epoch fencing |
| `.github/workflows/stage11-closing-price-production.yml` | Oracle source, source-epoch fencing, Oracle-based due-pick gate |
| `predictions-bot/predbot/config.py` | Correct strict-target documentation |
| `predictions-bot/predbot/selection/top.py` | Withhold below-band tickets; reject invalid odds |
| `predictions-bot/predbot/selection/daily.py` | Withhold below-band Daily tickets |
| `predictions-bot/predbot/jobs/settle.py` | Authoritative retry, All Predictions grading, ledger integration, recovered final events |
| `predictions-bot/predbot/jobs/ledger_settlement.py` | Scheduled immutable-contract grading and ticket completion |
| `predictions-bot/predbot/jobs/live_monitor_d1.py` | Stored-result ledger grading and durable completion metadata; preserve no-ledger no-op response |
| `predictions-bot/predbot/providers/espn.py` | Two verified competition/team-ID aliases |
| `predictions-bot/predbot/storage/db.py` | Document All-only grading versus published-only notifications/statistics |
| `predictions-bot/run_once.py` | Persist completion metadata compatible with existing dispatcher success detection |
| `predictions-bot/tools/production_health.py` | Read-only subsystem report, connectivity, authenticated Telegram, overdue results and nonzero unhealthy exit |
| `predictions-bot/tools/repair_oracle_ids.py` | Rehearsable, transactional ID/membership/cache/rejection recovery |
| `predictions-bot/tests/test_recovery_contracts.py` | Regression coverage for confirmed recovery defects and safety boundaries |
| `predictions-bot/tests/fixtures/espn_recovery_results.json` | Minimal recorded authoritative scoreboard facts and source URLs |
| `workers/bot/server-env.ts` | Match the monitor's 45-minute start window |
| `workers/bot/.env.example` | Document production notification timing |
| `workers/bot/ssh-relay/snapshot.py` | Consistent read-only SQLite backup/export preserving IDs and sequence |
| `workers/bot/ssh-relay/query-epoch.sh` | Pinned-SSH snapshot retrieval; remove unnecessary key diagnostics |
| `workers/bot/ssh-relay/receive-batch.sh` | Allow the restricted read-only `snapshot-get` command |
| `workers/scripts/d1_sync.py` | Monotonic immutable ledger updates and narrowly scoped completion metadata updates |
| `workers/bot/systemd/sureodds-bot.service` | Network-online ordering and continuing restart recovery |
| `workers/bot/systemd/sureodds-health.service` | Read-only health execution with the actual service environment |
| `workers/bot/systemd/sureodds-health.timer` | Ten-minute subsystem checks |
| `workers/migrations/0017_oracle_recovery_guards.sql` | Fail-closed database identity/timeout guards |
| `docs/ORACLE_RECOVERY_REPORT_2026-11-01.md` | Consolidated evidence, limitations and rollback |

## 9. Infrastructure and scheduling changes

Changed Oracle environment: `MATCH_START_WINDOW_MIN=45`; `GITHUB_REF=fix/oracle-production-recovery-20261101` pending default-branch approval. Installed the corrected bot unit and new health service/timer, reloaded systemd, and restarted only the bot. Delivery/post timers were briefly paused during atomic recovery and resumed. No cron jobs were added; `crontab` is not installed. No unrelated service was restarted.

| Task / function | Scheduler and Lagos frequency | Observed execution / result | Next expected from the 13:30 checkpoint |
|---|---|---|---|
| Telegram `pollLoop` | Continuous systemd bot | Restarted 13:05:25; polling resumed, PID 98266 | Continuous |
| Generation `run_once.py generate` | Actions stage7: 09:03 and 09:45; server dispatch fallback | Original November 1 generation completed 09:21; stored output recovered | November 2 scheduled path requires approved `main` update |
| `postGroupPicks` Top/Daily | Post sweep every 5 minutes, 09:00–23:55; publication gate 09:30 | November 1 original receipts retained; 13:30 sweep success | 13:35 |
| Live `live_monitor_d1.run` / start-stop detection | Delivery sweep every 5 minutes; demand dispatch interval 10 minutes | Repaired state advanced November 1; real start/end delivery confirmed | Demand-driven, approximately next 10-minute interval |
| Full `settle.run` and issued ledger | Actions stage9: 06:30 and 23:30; morning server fallback | Recovery run completed 13:12:23 with authoritative grading | Scheduled default-branch job awaits approval; Oracle dispatch uses fix branch |
| Recap / performance | Recap sweep every 5 minutes, 07:00–08:55 | November 1 last timer 08:55; service successful | November 2 07:00 |
| Watch / cleanup / admin health | Daily watch 11:00 | November 1 11:00:22; successful | November 2 11:00 |
| SQLite backup | Daily timer 04:00 | November 1 04:00:17; service exit 0; recovery backups separately verified | November 2 04:00 |
| Read-only production health | New timer every 10 minutes | Executed with nonzero unhealthy status for unresolved backlog, as intended | Ten minutes after the last health execution |
| Local Pro shadow closing | Disabled timer, unchanged | Last trigger October 28; Actions shadow workflow remains the active path | No local trigger scheduled |

Scheduled trigger times are configuration evidence, not a promise of GitHub runner start time. Exact last/next systemd timestamps were inspected. Historical recap content and every future scheduling cycle have not been independently reverified.

## 10. Tests: exact commands and actual results

Commands below run from `predictions-bot` unless a different directory is stated. Logs remain under `artifacts/` and are excluded from commits.

1. Final combined focused run:

   ```powershell
   .venv/Scripts/python.exe -m pytest tests/test_recovery_contracts.py tests/test_results_espn.py tests/test_espn_insight.py tests/test_settlement.py tests/test_phase3_settlement.py tests/test_settle_parallel.py tests/test_ledger_settlement.py tests/test_github_workflows.py tests/test_stage3_sync.py tests/test_closing_sync.py tests/test_rejection_sync.py tests/test_rejection_logging.py -q
   ```

   **242 passed in 127.22 seconds.** Subsequent compatibility verification: `-m pytest tests/test_live_monitor_d1.py tests/test_recovery_contracts.py -q` — **37 passed**. Latest recovery/workflow-only run before that compatibility adjustment: **44 passed**. No existing assertions were removed or weakened.

2. Full Python run: `.venv/Scripts/python.exe -m pytest -q` — **3,531 passed, 64 failed, 18 errors, 507.97 seconds**. **RED, not accepted as a passing suite.** Many tests explicitly require below-target fallback or deadline-only voids; privacy/cache fixtures also depend on those former contracts. One no-op monitor response regression exposed by this run was corrected afterward and its original test now passes. The full suite has not been rerun after that correction or migrated wholesale to the new contract. Log: `artifacts/recovery-python-final.log`.

3. Oracle adapter/relay: `npm test` from `workers/bot` — **69 passed, 0 failed**. Initial sandbox subprocess restrictions were resolved by running with approved access.

4. Worker integration: `npm test` from `workers` — **571 passed, 1 failed**. The failed test requires `STAGE3_CRON_ENABLED="1"` in `wrangler.toml`; the pre-existing post-cutover configuration intentionally sets it to `0`. It was not re-enabled to obtain a green test.

5. Build: `npm run check` from `workers` — **passed**, Wrangler dry run. No Cloudflare deployment occurred.

6. Runtime lint, with isolated tool cache on `PYTHONPATH`:

   ```powershell
   .venv/Scripts/python.exe -m ruff check --select E9,F63,F7,F82 predbot/jobs/settle.py predbot/jobs/ledger_settlement.py predbot/jobs/live_monitor_d1.py predbot/providers/espn.py predbot/selection/top.py predbot/selection/daily.py tools/production_health.py tools/repair_oracle_ids.py run_once.py ../workers/bot/ssh-relay/snapshot.py ../workers/scripts/d1_sync.py
   ```

   **All checks passed.** This is scoped runtime-error lint, not a claim of repository-wide stylistic lint compliance.

7. Targeted typing:

   ```powershell
   .venv/Scripts/python.exe -m mypy --follow-imports=skip --ignore-missing-imports --check-untyped-defs tools/production_health.py tools/repair_oracle_ids.py predbot/jobs/ledger_settlement.py ../workers/bot/ssh-relay/snapshot.py
   ```

   **Success: no issues found in 4 source files.** This is not a full-application type-check claim.

8. Production checks: `python -m compileall -q predbot run_once.py tools/production_health.py tools/repair_oracle_ids.py`, `bash -n` on the two relay shell scripts, `systemd-analyze verify` on installed bot/health units, SQLite backup/`integrity_check`/`foreign_key_check`, real provider probes and real delivery receipts. Relevant checks passed; health deliberately exits **1** while the backlog remains unresolved.

## 11. Production verification and observability limits

Oracle deployment was verified at **`9c5d3090abb686260cf5729933a37617082e48bd`**, with the bot, delivery timer, post timer and health timer all active. Subsequent documentation-only commits do not change that functional revision. The main successful backlog run executed the earlier `92ed5e5` recovery revision; later changes added guards, exact aliases, diagnostic recovery and compatibility fixes. Successful [monitor run 41303160326](https://github.com/preshious11/Sureodds/actions/runs/41303160326) established that the Oracle snapshot path advances live state. An earlier recovery settlement attempt correctly failed at apply on a moved source epoch rather than silently declaring success; full computation was then placed under the shared lease.

Oracle-origin checks: ESPN HTTP 200; Telegram `getMe` HTTP 200 with authenticated success; authenticated Odds API quota probe HTTP 200. The periodic health check's unauthenticated Odds transport request correctly reports HTTP 401 as reachable but not authenticated; it does not spend credits or pretend to validate provider-account authorization. Provider credentials used by Actions are not copied into health logs.

Health covers database integrity/WAL, disk, process/timers, current generation stages, provider coverage/usage diagnostics, latest fixture check, Telegram authentication, connectivity and overdue pending rows. A successful heartbeat does not override a failed subsystem. At 13:23:56 Lagos it correctly reported unhealthy with 323 overdue predictions, despite an active bot and reachable providers. Its raw HTTP exception strings are excluded to avoid credential-bearing URLs.

GitHub run/step status and production database evidence were accessible. Detailed log-download redirects were not consistently accessible: Oracle attempts returned HTTP errors, and a workstation redirect encountered DNS failure. No claim of a complete external-account/billing or every-provider historical audit is made.

## 12. Remaining issues and acceptance gaps

1. **323 overdue predictions remained unverified at the health checkpoint.** Keep them pending. Some have unsupported competition coverage; two proven naming defects were repaired, but remaining fixtures have not all been individually classified. Further authoritative coverage/mapping work is needed before claiming the historical backlog fully resolved.
2. **Three issued tickets remain unresolved.** Two legs were future fixtures; one historical tennis result was not verified. The ledger now executes and settles available evidence rather than remaining entirely unwired.
3. **The full Python suite and one Worker test remain red.** The changed product contracts and dependent fixtures require reconciliation; no passing-full-suite claim is made.
4. **Default-branch scheduling remains an approval dependency.** Oracle dispatches the fix branch, but GitHub's existing scheduled workflows on `main` retain their older code until the PR is approved/merged. Database guards reject the two confirmed unsafe write patterns; this is fail-closed protection, not a substitute for completing the merge.
5. Exact provider billing reset date/time was unavailable in returned quota headers. Current usable credits are verified; the calendar reset is not assumed.
6. The next fresh daily generation/publication, a full future recap cycle, unexpected-crash fault injection, remote Object Storage restore, and comprehensive statistical/model validation are not claimed proven by this recovery.

## 13. Deployment status

**Live on Oracle's dedicated recovery branch**, with corrected environment, restarted bot, active delivery/post/recap/watch/backup timers, active new health timer, restored records, authoritative settlement and actual group/owner delivery. Local Pro closing remains disabled. Public publishing gates and automated ordinary-subscriber private delivery were not enabled.

**Not merged into `main`.** Automatic approval review rejected the direct default-branch push because the dedicated-branch instruction did not constitute specific approval for that mutation. Draft PR #4 is the reviewable change. Final acceptance remains open for the issues above; this report is evidence of a substantial live recovery, not a claim that every acceptance criterion is green.

## 14. Rollback — exact recovery procedure

Rollback restores the original pre-recovery deployment, including its known defects. Prefer retaining the database safety guards and investigating a specific regression where possible. The procedure below has **not** been executed. It intentionally interrupts service and SureOdds-user SSH access while preserving the separately verified Ubuntu administrator login.

Use the verified Ubuntu administrator key. Before restoring, cancel active generation/monitor/settlement/backfill/closing runs in GitHub Actions and verify they are terminal. Then, on Oracle:

```bash
sudo systemctl stop sureodds-deliver-sweep.timer sureodds-post-sweep.timer \
  sureodds-recap-sweep.timer sureodds-watch.timer sureodds-backup.timer \
  sureodds-health.timer
sudo systemctl stop sureodds-bot.service sureodds-deliver-sweep.service \
  sureodds-post-sweep.service sureodds-recap-sweep.service \
  sureodds-watch.service sureodds-backup.service sureodds-health.service

# Prevent new forced-command relay connections during restore.
# Keep this administrator SSH session open; its Ubuntu key is unaffected.
sudo chmod 000 /home/sureodds/.ssh/authorized_keys
sudo pgrep -af 'receive-batch.sh|apply-plan.ts|snapshot.py'
```

The last command must show no active application relay/snapshot writer; wait for any existing job to finish before continuing. Save a fresh SQLite backup of the recovery state if it may be needed. Restore with SQLite's backup API, not a live file copy:

```bash
sudo -u sureodds /usr/bin/python3 - <<'PY'
import sqlite3
source = sqlite3.connect('file:/var/lib/sureodds/recovery-20261101/before.db?mode=ro', uri=True)
target = sqlite3.connect('/var/lib/sureodds/sureodds.db')
target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
source.backup(target)
assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
target.close()
source.close()
PY

sudo -u sureodds git -C /opt/sureodds switch --detach a0271358f169848e598ccc1219562442b77a4751
sudo cp /var/lib/sureodds/recovery-20261101/bot.env.before /opt/sureodds/workers/bot/.env
sudo chown sureodds:sureodds /opt/sureodds/workers/bot/.env
sudo chmod 600 /opt/sureodds/workers/bot/.env
sudo cp /var/lib/sureodds/recovery-20261101/bot.service.before /etc/systemd/system/sureodds-bot.service
sudo systemctl disable sureodds-health.timer
sudo systemctl daemon-reload
sudo chmod 600 /home/sureodds/.ssh/authorized_keys
sudo systemctl start sureodds-bot.service sureodds-deliver-sweep.timer \
  sureodds-post-sweep.timer sureodds-recap-sweep.timer \
  sureodds-watch.timer sureodds-backup.timer
sudo systemctl is-active sureodds-bot.service
sudo systemctl list-timers --all --no-pager 'sureodds-*'
```

Restoring `before.db` also removes the additive guards and reverses the recovered records/settlements. For a code-only rollback, do not restore the database or drop its guards; retain the repaired data and pause incompatible stale workflows. Do not re-enable the retired Cloudflare application's cron as part of this rollback. No default-branch revert is needed because `main` was not changed.
