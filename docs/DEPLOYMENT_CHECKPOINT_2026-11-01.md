# November 1 deployment and fresh-ticket checkpoint

Status: **blocked, not merged, not deployed to main, no fresh Telegram publication**.

## Verified production state

OCI `/opt/sureodds` is clean and runs `9c5d3090abb686260cf5729933a37617082e48bd` on the recovery branch. Remote main remains `a2cb618c05ec109771d297a100765c19dcb197db`. The bot is active/running, PID 98266, zero automatic restarts, started at 12:05:25 UTC. Delivery, post and health timers are active. Delivery/post journal entries at 13:00 and 13:05 UTC completed successfully. Image validation logged a text-card fallback; that is not a successful fresh publication.

Fresh rollback backup: `/var/lib/sureodds/deployment-20261101T125742Z/before.db`, 90,677,248 bytes, SQLite integrity `ok`, SHA256 `241b42e647b5ea5ae3394bd8120abd5b8dad3f606a6e7edbc6b1c7f237a5b2af`. The same private directory contains the preceding commit, service definitions and mode-0600 environment copy. No production code, schema, environment or service restart was applied during this checkpoint. Production quick-check remained `ok`.

## Merge review

[PR #4](https://github.com/preshious11/Sureodds/pull/4) is draft and conflict-free but GitHub reports `mergeable_state=unstable`. At reviewed head `59f7583461748bfe04ecea7d97452250b2f24bea`, schema compatibility, monitoring and fresh evaluation passed; `worker-foundation` and `engine-offline` failed. No review approvals were recorded. This does not satisfy the requested confirmation of no unresolved blockers, so main was not modified.

The Worker test at `workers/tests/match_event_eligibility.test.mjs:389` still requires the retired Cloudflare cron gate to equal 1, while main deliberately sets it to 0. The Python reproduction confirms contract conflicts: tests require below-target tickets and voiding without authoritative results, contrary to the recovery requirements. These checks need reconciliation with the new contracts and successful complete execution; focused passes do not substitute for that work. No assertions were removed and no publishing gates or thresholds were weakened.

## Fresh evaluation

[Actions run 41309484981](https://github.com/preshious11/Sureodds/actions/runs/41309484981) ran the recovery head above and succeeded. It used already-configured Actions provider secrets and a consistent read-only Oracle export. Provider cache was removed **only in a disposable runner copy**. Historical forecasts supplied model context; ticket selection used a separate fresh scratch pool. Neither production apply nor Telegram publication ran.

Generation: 13:05:03–13:10:15 UTC (14:05–14:10 Lagos). **48 fixtures discovered, 48 supported, 48 normalized**: 25 football, 8 tennis, 4 basketball, 11 American football. OddsAPI reported complete; SharpAPI and SportsGameOdds reported partial coverage. The engine accepted the slate under the existing provider-fallback policy. Coverage degradation was not hidden as an empty slate.

Top evaluation, actual product **2.0335**:

| Selection | Price | Kickoff, Lagos |
| --- | ---: | --- |
| Lyon to beat Angers | 1.4 | November 1, 17:15 |
| Lazio to beat Cagliari | 1.4525 | November 1, 15:00 |

Daily evaluation, actual product **10.0170804626025**:

| Selection | Price | Kickoff, Lagos |
| --- | ---: | --- |
| Paris FC–Monaco over 2.5 goals | 1.75 | November 1, 17:15 |
| Boston Celtics to beat Orlando Magic | 1.4 | November 1, 21:40 |
| New York Red Bulls +1.5 against Nashville | 1.6667 | November 1, 22:30 |
| Jiri Lehecka to beat Valentin Vacherot | 1.499 | November 1, 15:30 |
| Lyon–Angers over 2.5 goals | 1.6365 | November 1, 17:15 |

Every evaluated selection had a future kickoff when evaluation completed. These prices must be rechecked before any later publication; this report is not permission to replay an expired evaluation. No model weights, confidence floors, price limits, diversity rules or eligibility gates were changed.

The run retained private artifact `fresh-evaluation` (artifact ID 13084444775), containing `report.json`. The API annotation confirmed the counts and selections above, but GitHub truncated that individual annotation at 4,096 characters; subsequent code splits annotations into bounded, reconstructable parts with regression coverage. Direct artifact download through the available repository credential returned HTTPError; the complete artifact was not independently downloaded locally.

## Exact persistence and publication blockers

1. Actual production `getAllPicksForCollection`, `renderView` and `renderGroupCard` still return the morning contracts: Top 2 legs / **2.0088**, Daily 5 legs / **10.387689562882322**. Daily includes Como–Venezia, kickoff 11:30 UTC, already started. Those records are not the fresh evaluation and were not republished.
2. `PredictionPool.package` refuses to replace Top/Daily memberships after a day-level Top/Daily receipt exists. The normal generation workflow does not pass `--force`; even a forced snapshot refresh does not bypass that membership freeze.
3. `store_ticket_prediction` reuses an existing exact forecast, preserving its original price. The exact-selection unique index does not include a forecast revision. For example, the fresh Lazio price 1.4525 must not overwrite the recorded 1.44 contract.
4. `postGroupPicks` uses the atomic `(kind, post_date)` receipt. November 1 group Top **437**, Daily **438**, and both owner-copy receipts already exist at 08:30 UTC. Ordinary posting therefore skips another card. These original receipts were preserved. They are **not evidence of fresh delivery**.

The unresolved work is a reviewed publication-revision path that preserves earlier forecast prices, memberships and receipts while allowing new current-day contracts to be retrieved through the same `/top` and `/daily` paths, followed by complete passing checks and real Telegram confirmation. Deleting receipts, force-repricing old forecasts or sending the isolated evaluation directly would evade the requested production path and duplicate/history protections. No access or permission prompt is claimed to solve this implementation gap.

Production records at the checkpoint: 3,082 predictions, 3,215 memberships, 502 posts, 164 match events; no fresh forecast rows or publication receipts were written. Generation results are persisted only in the evaluation artifact, not the authoritative production database.

## Tests actually run during this checkpoint

From `predictions-bot`:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_all_predictions_architecture.py tests/test_architecture_boundaries.py tests/test_live_results.py tests/test_settlement_lifecycle.py -q --tb=short
```

**133 passed, 30 failed in 4.60 seconds.** The output is retained locally at `artifacts/deployment-blockers.log`. This is a blocker reproduction, not a successful full-suite result.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_fresh_evaluation.py tests/test_recovery_contracts.py tests/test_github_workflows.py -q
```

**47 passed in 1.05 seconds** after the annotation fix. Tests cover strict-band selection, started/below-target rejection, unchanged source predictions, no production apply/Telegram send, lossless bounded diagnostics, and the existing recovery/workflow contracts.

`git diff --check` passed. No runtime dependency manifests changed. No dependency installation or production restart was required because deployment did not proceed.

## Changed files

- `.github/workflows/stage5-acceptance-production.yml`: explicit fresh-evaluate operation, isolated read-only snapshot job, existing Actions secrets, private report artifact; ordinary acceptance/apply job is skipped for that operation.
- `predictions-bot/tools/fresh_evaluation.py`: fresh quotes and strict selectors in disposable databases, no publication/apply, artifact and bounded diagnostics.
- `predictions-bot/tests/test_fresh_evaluation.py`: isolation, eligibility, odds-band and annotation regression tests.
- This checkpoint report: actual evidence and remaining implementation gap.

Unrelated local document deletions and untracked work were preserved. No infrastructure configuration changed. No destructive operation or recovery rollback is needed: production remained on its preceding commit and database. If a later deployment is attempted, take another fresh SQLite backup and establish that deployment's rollback before applying it.
