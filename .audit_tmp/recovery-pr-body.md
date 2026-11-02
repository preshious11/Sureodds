Production computation read stale Cloudflare D1 snapshots while applying its diffs to Oracle. After cutover, reused integer IDs silently disconnected caches, predictions, and ticket memberships; monitoring and settlement could report success without processing the Oracle records.

Read consistent Oracle snapshots for server-target workflows, fence against the source epoch, and acquire the shared lease before full computation. Enforce configured combined ticket odds, grade all predictions and immutable issued contracts using authoritative results, retry delayed results without timeout-only voids, restore missed final events, align start timing, and add subsystem health checks plus fail-closed database guards. Two verified ESPN team-name mappings are scoped to exact league/team IDs.

Verified live recovery:
- 414 predictions restored; 424 cache references remapped; 4 incorrect memberships corrected; 986 rejection diagnostics restored.
- 2,362 predictions graded in the main recovery run, plus 10 through two verified historical fixture mappings. 193 unverified timeout voids were reopened; authoritative cancellations were preserved.
- 37 issued tickets settled; 3 unresolved, including future fixtures.
- Real start delivery confirmed to group and owner. Thirteen recovered end events delivered independently to both destinations; repeat sweep did not duplicate them.
- 3,188 cached references verified, zero foreign-key violations or orphan memberships, integrity checks passed.
- November 1 Top: 2.0088; Daily: 10.38768956288232. Oracle-origin authenticated Odds API probe: HTTP 200, 491 credits remaining.

Validation: 242 combined focused regressions passed; subsequent original no-op compatibility plus recovery checks: 37 passed. Oracle adapter/relay: 69 passed. Scoped runtime lint and four-file type check passed. Dry-run build passed. The last full Python run remains RED: 3,531 passed, 64 failed, 18 errors, including legacy below-target/timeout-void expectations and dependent fixtures; one return-contract regression found there was subsequently fixed. Worker suite: 571 passed, one test conflicts with the existing disabled Cloudflare cron. No original assertions were removed or weakened.

Oracle runs the recovery branch and additive guards. Default-branch mutation awaits explicit owner approval. Remaining acceptance gaps include 323 overdue predictions without verified results, the full regression suite, and the next fresh scheduled generation/recap cycle.

Full evidence, every changed file, infrastructure details and exact rollback: [Oracle recovery engineering report](https://github.com/preshious11/Sureodds/blob/fix/oracle-production-recovery-20261101/docs/ORACLE_RECOVERY_REPORT_2026-11-01.md).
