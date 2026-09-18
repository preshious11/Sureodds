# SQLite prediction cache implementation

## Evidence and previous flow

**SQLITE CANONICAL SNAPSHOT CACHE PRESENT BEFORE CHANGE: NO.**
SQLite already contained individual Top/Daily rows in `picks`, subscribers,
settings, budgets, results, and `generation:<date>` metadata in `kv`. There was
no `prediction_cache` table. A read-only check of the configured local database
also confirmed that this table was absent before application startup migration.

Previous interactive flow:
`command/button -> external cache / JSON file / memory -> existing SQLite picks -> providers`.
A process restart could reconstruct a card from existing `picks`, without API
calls, but that was not a persisted canonical generation with retryable provider
portions. Any existing picks could stop regeneration even if football was missing.
Scheduled publication separately ran `daily_picks.run`, then reconstructed the
interactive cache from picks. The unpublished football cache experiment also
made failed collections terminal for the day; that implementation is replaced.

## New runtime flow

`/top`, `/daily`, buttons and pagination call:

```text
callbacks.open_view
  -> UnifiedFetcher.get_daily_snapshot
  -> Database.get_prediction_cache
  -> SQLite prediction_cache
```

Scheduled channel publication calls `daily_picks.run(..., service=service)`, which
uses that exact same service. Subscriber broadcasts call `service.published_snapshot`,
which delegates to `get_daily_snapshot`. No second independent generator is used.

```text
request -> SQLite lookup for application timezone/date
  COMPLETE and unexpired -> return stored snapshot (zero provider calls)
  PARTIAL before cooldown -> return stored usable snapshot (zero provider calls)
  missing/stale/due PARTIAL -> shared per-database in-process lock
    -> daily_picks.build_picks
    -> ProviderChain.collect -> collect_persisted
    -> reuse completed SQLite provider portions; fetch failed/missing portions
    -> normalize -> existing probability model -> existing selector
    -> Top and Daily in one DailySnapshot
    -> SQLite transaction: retain settlement picks + upsert canonical snapshot
    -> return selected view
```

SQLite is authoritative on every request. There is no memory/file/external-cache snapshot
that can override a newer SQLite row. An existing legacy `picks` row does not
prove source coverage, so the first request after upgrade creates a new canonical
snapshot instead of permanently importing a potentially incomplete card.

## Schema and database API

Additive `CREATE TABLE IF NOT EXISTS` statements run in `Database.__init__`:

| Table | Columns |
| --- | --- |
| `prediction_cache` | `cache_key` PRIMARY KEY, `local_date`, `timezone`, `status`, `payload_json`, `sport_status_json`, `generated_at`, `updated_at`, `expires_at`, `retry_after`, `cache_version` |
| `provider_cache` | `cache_key` PRIMARY KEY, `local_date`, `payload_json`, `metadata_json`, `expires_at`, `cache_version` |

A canonical key is `daily_predictions:Africa/Lagos:2030-06-01`. One upserted row
holds both lists. Snapshot version is **2**; provider data version is **1**.
Provider rows contain normalized events and coverage metadata. API-Football raw
successful responses use separate keys in the same `provider_cache` table.

Storage-layer methods: `get_prediction_cache`, `upsert_prediction_cache`,
`delete_prediction_cache`, `prune_prediction_cache`, `defer_prediction_retry`,
`get_provider_cache`, `upsert_provider_cache`, and `transaction`.
No prediction-cache SQL lives in Telegram handlers.

JSON is deterministic (`sort_keys=True`) and rejects NaN/infinite numbers.
Real kickoff times are stored as UTC ISO timestamps. Records preserve provider,
event ID, market/period/stat/entity, participants, sport, league, selection,
probability, price and contributing sources. Top odds are included in the JSON
and remain reproducible from the unchanged selection prices. The existing selector
stores the additional Daily selections separately from Top: the Daily ticket is
Top plus those additional selections; the Top view reads only Top.

## Status, partial retry and credit preservation

- **COMPLETE**: every enabled provider in the configured collection order completed,
  including valid empty results. This does not imply every sport has eligible picks
  or every league in the world is covered by the configured provider plans.
- **PARTIAL**: any required source failed or reported incomplete coverage; no enabled
  sources also yields PARTIAL. Usable predictions are still displayed.
- Sport/provider **success**: applicable events were returned.
- Sport/provider **empty**: source completed without applicable events. Fetch and
  scoring counters distinguish no applicable events from candidates rejected by
  the selection rules. Empty is not a reason for endless retry.
- Sport **failed**: one of its required sources is incomplete. Available events can
  still exist; they are retained, but the status is not promoted to complete.

Football fails / baseball succeeds: save a PARTIAL baseball card and both provider
states. Requests during `CACHE_RETRY_SECONDS` (default **900**) read SQLite only.
On the next eligible request or scheduled delivery poll, retry the failed football
source; reuse stored baseball even across a process restart. Combine source events,
rerun the unchanged model/selector, then atomically replace both lists and status.
Source retries operate at provider granularity; a mixed-sport provider may need to
repeat its own fetch, but separate completed providers are never refetched.

API-Football successful generation responses are cached by day, account/host hash,
endpoint and parameters. A later failed-source retry reuses successful fixture,
odds-page and optional prediction responses. Failed responses remain retryable.
HTTP retries are disabled for that provider; attempted requests, including errors,
count against the local budget. The existing daily quota/settlement reserve still
applies. A genuinely exhausted daily quota cannot be recovered by clearing cache.
Settlement queries deliberately remain separate because final results change.

`get_daily_snapshot(force_refresh=True)` bypasses the canonical snapshot and
rebuilds selection, allowing failed portions to retry before their cooldown.
Completed provider responses are still reused to conserve credits. Existing admin
controls remain restricted; no expensive public refresh command was introduced.

On generation failure, the last valid snapshot remains readable and retry is
throttled. No usable data means the existing view returns an unavailable message.
Partial usable data is not hidden. Already-delivered Telegram broadcasts are not
resent or edited on recovery; existing delivery deduplication is preserved.

## Atomicity, concurrency, expiration and retention

Both lists, generation metadata and COMPLETE/PARTIAL status live in one JSON row.
SQLite transactions commit new settlement selections and the snapshot together;
an injected failure after the SQL upsert rolls back to the prior snapshot.
Existing settlement/history rows are retained; existing matching selections are
not inserted again during ordinary regeneration. Settlement and stats formulas
are unchanged.

All service instances pointing at the same file in one Python process share an
RLock. It covers the missing/due check and generation, avoiding duplicate API
batches among scheduler, command and broadcast threads. This is intentionally a
single-bot-process design, not a multi-host distributed lock.

Validity checks include date, application timezone, version, JSON and expiration.
Africa/Lagos midnight is 23:00 UTC on the preceding UTC date. Subscriber timezones
only change presentation; users share the application-day dataset. New dates use
new keys. Cache history older than `CACHE_RETENTION_DAYS` (default **7**) is pruned
on writes, including provider response rows. Historical picks, subscribers,
settings, budgets and results are not pruned by cache maintenance.

## Database path and hosting evidence

- Dockerfile: `WORKDIR /app/predictions-bot`, **`DB_PATH=/data/predbot.db`**.
- Config fallback without that environment override: **`./data/predbot.db`**, relative
  to the application's working directory.
- The local app environment resolves to
  `C:/Users/VICTUS/OneDrive/predictions-bot/predictions-bot/data/predbot.db`.
- The cache uses the very same Database object/path as subscribers. No `cache.db`
  or temporary production database is created.
- The repository has a Dockerfile and Railway start configuration, but no evidence
  of a persistent JustRunMyApp volume or its live environment overrides.

**Persistence across container replacement must be verified in JustRunMyApp.**
Mount the existing database's storage persistently. Do not switch DB_PATH to an
empty location and expect existing subscribers to move automatically. A normal
process restart reuses the file; losing the file means losing its cache and causes
fresh provider collection. The implementation cannot make an ephemeral disk durable.
Startup logs the resolved path/existence and `Prediction cache backend=sqlite`.

## Changed files

| File | Purpose |
| --- | --- |
| `predbot/storage/db.py` | Additive tables, cache API, transactions, shared generation lock, startup path log |
| `predbot/cache/fetcher.py` | Canonical SQLite hit/miss/retry/force-refresh service and atomic save |
| `predbot/cache/providers.py` | Persistent normalized provider portions, retry and per-sport coverage metadata |
| `predbot/cache/models.py` | Version 2 snapshot, status, sport metadata, complete pick identity and Top odds |
| `predbot/cache/__init__.py` | Remove obsolete backend imports |
| `predbot/cache/manager.py` | Removed the external-cache/file/memory prediction backend |
| `predbot/providers/chain.py` | Route runtime collection through persisted provider portions |
| `predbot/providers/api_football.py` | SQLite response reuse and conservative request accounting |
| `predbot/jobs/daily_picks.py` | Shared service for publication, retain provider coverage, avoid repeat pick inserts |
| `main.py` | One service shared by commands, schedule and admin refresh |
| `predbot/bot/pagination.py` | Serve usable partial picks; unavailable text only for empty partial views |
| `predbot/bot/broadcast.py` | Update documentation to match the shared service; delivery rules unchanged |
| `predbot/config.py`, `.env.example` | SQLite retry/retention settings; remove runtime external-cache configuration |
| `requirements.txt` | Remove the external-cache client dependency |
| `diagnose_prediction_cache.py` | Read-only SQLite diagnostics; no sports API calls or credential output |
| `tests/test_sqlite_prediction_cache.py` | New persistence, subprocess restart, recovery, atomicity, concurrency and migration tests |
| `tests/test_cache_pagination.py`, `tests/test_subscribers_broadcast.py` | Test routing/pagination against SQLite rather than the removed backend |
| `tests/test_generation_regressions.py`, `tests/test_quota_handling.py` | Usable partial data and quota-state regression expectations |
| `tests/test_prediction_layout.py`, `tests/test_prediction_timezone.py` | Adapt construction to the SQLite service; retain layout/timezone checks |
| tests for the removed external-cache backend | Removed as obsolete; a repo-wide guard test now keeps it out |
| `README.md`, `PREDICTION_GENERATION_REPORT.md`, `SQLITE_CACHE_REPORT.md` | Current operating instructions and historical-report correction |

The uncommitted earlier external-cache diagnostic/football-cache experiment was also removed.
No welcome copy, polling, subscription semantics, selection math, grading or stats
formulas were changed. Existing POST_TIME scheduling is preserved (this repository's
default remains 09:00 Africa/Lagos, not changed to 09:30).

## Test evidence

- Focused cache/routing/provider regression command: **63 passed**.
- Full `python -m pytest -q`: **232 passed**.
- Restart test: **PASS**, including a completely new Python subprocess.
- Provider calls after a valid SQLite COMPLETE cache hit: **0**.
- Football and baseball, both ticket lists, identifiers, confidence, odds and UTC
  timestamps survive persistence. Top combined odds remain **2.03** in the test.
- Temporary football failure recovers after the cooldown without repeating baseball.
- Covered: miss/hit, valid empty, all-source failure, stale version/JSON/expiry,
  force refresh, rollback, concurrent separate Database instances, fallback,
  local midnight, retention, non-destructive migration and no external-cache package.
- No remaining test failures. Earlier tests expecting the removed external-cache backend or
  provider-wide blocking were updated to the requested SQLite/partial-serving behavior.
- No live sports API requests or Telegram messages were made for these checks.

## External cache check

No external cache service is imported or required by application runtime, and its
client is removed from requirements. A leftover `CACHE_DIR` value, or any old
cache-service URL, left in local/host environments is ignored (the startup log lists
unread keys by name). Local installed packages need not be uninstalled.

## Deployment and live verification

1. Push these changes, then redeploy/restart the bot on JustRunMyApp. These edits
   have not been pushed by this task.
2. Preserve the existing SQLite file and ensure its configured directory is on
   persistent storage. With the repository Dockerfile the intended path is
   `/data/predbot.db`; verify the actual startup log and hosting mount.
3. No manual database deletion, cache clearing, or SQL migration command is needed.
   Startup creates the new tables non-destructively. The first request/publication
   builds today's canonical snapshot automatically. Old external/file caches are
   ignored. If today's football quota is already exhausted it stays PARTIAL until
   the provider can recover; available picks still display.
4. Remove the obsolete `CACHE_DIR` setting, and any leftover cache-service URL setting, if desired. Optional settings:
   `CACHE_RETRY_SECONDS=900`, `CACHE_RETENTION_DAYS=7`. Keep DB_PATH, provider keys,
   subscriber timezone configuration and POST_TIME as before.
5. Confirm startup logs: `SQLite database path: ... | exists=...` and
   `Prediction cache backend=sqlite | ... | Cache version=2`.
6. Send `/daily`, then `/top`, then navigate both inline views. First generation logs
   `Cache MISS` / provider retries / `Cache UPDATED`. Repeated COMPLETE reads log
   `Cache HIT ... status=COMPLETE`, without new `Cache RETRY` or provider HTTP calls.
   Both views must have the same snapshot date/generation; Top shows only Top.
7. From the application directory in the same environment run:
   `python diagnose_prediction_cache.py`. It prints the resolved file, row status,
   generation/expiry/retry timestamps, sport coverage, and both list counts. It
   opens SQLite read-only and never calls sports APIs or triggers migration.
8. Restart the process with the same persistent file. Repeat `/daily` and `/top`:
   the generation timestamp stays unchanged and logs show SQLite hits. For PARTIAL
   rows, retries may occur once cooldown has elapsed; COMPLETE rows never refetch.
9. For recovery, inspect `sport_status` and the retry timestamp. After cooldown,
   the next request/delivery poll retries the failed provider and logs a new atomic
   snapshot. Use internal `force_refresh=True` or the existing authorized admin
   refresh only when intentionally testing early recovery.

The local code and tests are verified. The live JustRunMyApp process, volume mount,
and its actual provider availability have not been accessed or asserted healthy.
