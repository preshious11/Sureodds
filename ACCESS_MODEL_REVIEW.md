# Access model and result GIF review

## Approved behavior

Group navigation contains four private links: All, Top, Daily and Custom. Statistics, Yesterday, recap and GIF administration are private admin functions. Identical Top predictions are excluded from Daily; other markets on the same fixture remain eligible.

Result GIFs follow the final confirmed group END alert, using shared result_gif_top/result_gif_daily claim/confirm/release receipts. One aggregate covers the batch's relevant tickets. Queued END alerts must be confirmed before the GIF. POST (*/5 8-22 UTC) provides the settlement-without-END backup. Final deadline-expired voids are neutral: wins plus voids produce WON; all-neutral tickets get a receipt without a GIF.

The batch is six match events. DELIVER is bounded at 40 D1 queries: 11 event/context/receipt operations + 17 result/notice operations + 12 reporting/recovery operations. POST is bounded at 35: 18 publication operations + 17 result/notice operations. Real SQLite budget tests assert both maxima.

MATCH_START_WINDOW_MIN is 45 in both the Worker and stage8 workflow. Boundary tests cover 45 minutes accepted and 46 expired. The kickoff backlog test includes 13 minutes of monitoring latency and five-minute sweep intervals. A workflow test prevents the settings diverging.

## Validation

- Full Worker suite after the 45-minute change: 402 passed.
- Workflow and live-monitor Python tests after the change: 17 passed.
- Prior full Python suite: 3262 passed.
- Prior Wrangler build dry-run passed.
- git diff --check passed.

Tests cover delayed two-leg finishes, shared Top/Daily fixtures, queued-END ordering, the POST backup, either race winner, kickoff backlog draining, deadline-void mapping and query ceilings. Existing cron mocks model batch context and receipt operations; eligibility expectations now allow six events per sweep. Python selection tests and golden diagnostics reflect exclusion of identical Top picks from Daily.

## Rollout steps 1-4 (approved)

1. Apply only workers/migrations/0012_result_gifs.sql using wrangler d1 execute sureodds_production --remote --file migrations/0012_result_gifs.sql, then verify result_gifs and idx_result_gifs_kind in sqlite_master. Completed successfully on 2026-10-18. No other migration was applied.
2. Check the Lagos date and set RESULT_GIFS_START_DATE. Set to 2026-10-18.
3. Commit the approved picker, Python tests, Worker sources/tests, migration 0012, Worker config, stage8 workflow and this report. Commit message: Restrict bot access and add settled-ticket GIF replies. Push main.
4. Deploy with wrangler 4.151.1 using workers/wrangler.toml, then verify the active version with deployments list.

GIF registration and Telegram menu refresh are separate later manual steps; this rollout does not send those commands.
