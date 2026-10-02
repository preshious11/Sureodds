# Live match tracking and immediate settlement

Implemented on stored picks, with no changes to prediction generation, selection,
odds targets, confidence, snapshot freezing, prediction/provider caches, commands,
09:30 card delivery, or the morning recap.

## Files changed

Paths are relative to `predictions-bot/`:

- `.env.example`
- `main.py`
- `predbot/config.py`
- `predbot/providers/base.py`
- `predbot/providers/espn.py`
- `predbot/providers/football_data.py`
- `predbot/providers/chain.py`
- `predbot/storage/db.py`
- `predbot/jobs/settle.py`
- `predbot/jobs/live_results.py` (new)
- `predbot/bot/broadcast.py`
- `predbot/bot/format.py`
- `tests/test_results_espn.py`
- `tests/test_live_results.py` (new)
- `LIVE_RESULTS_REPORT.md` (new)

## ESPN bug

Previously, any nonterminal status fell through to `in_progress`, including
`STATUS_SCHEDULED`. Scheduled fixtures now return `scheduled`; `state=pre` and
`state=in` distinguish scheduled/live. Final/completed, postponed, and cancelled
statuses are handled explicitly; unrecognized statuses return `unknown`.
Existing score parsing and beyond-regulation safeguards remain intact.
Football-data's `SCHEDULED` and `TIMED` also map to `scheduled`.

## Live tracker and API usage

- Enabled by default with `LIVE_UPDATES_ENABLED=true`, `LIVE_POLL_MINUTES=5`.
  An interval below one minute is rejected.
- Reads the existing `picks` table, grouping attached picks by fixture identity
  and publication date. A per-run result cache uses `fixture_key`, so duplicate
  tiers, subscribers, and publication dates do not multiply source lookups.
- Starts checking ten minutes before kickoff, continuing within the existing
  four-day `MAX_PENDING_DAYS` window. Kickoff alone never proves a start.
- Source priority: ESPN; football-data for a mapped supported competition when
  ESPN cannot supply status; EuroLeague for confirmed finals only.
- EuroLeague's existing `played=false -> in_progress` value is deliberately
  ignored for start notifications.
- ESPN's competition/day scoreboard cache is shared across selected fixtures.
  Provider caches are initialized once per run as needed. No additional games
  from those scoreboards become live watches.
- OddsAPI `/scores` and SportsGameOdds are never queried for ordinary live
  status. Existing result paths can enrich an ungradable **confirmed final**,
  with responses shared across its picks. Missing stats remain pending for
  the existing settlement/recap safety net; they are never guessed or lost.
- Terminal fixtures stop status polling. Persisted unfinished settlement or
  delivery is resumed separately. Completed terminal work does nothing.
- The threaded IntervalJob is optional and does not run synchronously during
  startup. A resolver run lock prevents overlapping live polls and interference
  with fallback settlement's result-provider caches.

One fixture gets one grouped status-resolution pass per poll. Source fallbacks
or ESPN's existing previous-day lookup can require additional HTTP requests;
duplicate picks and subscriber counts never cause those extra requests.

Lifecycle logs follow this sequence (illustrative counts):

```text
live: tracking 1 fixtures
live: Chelsea vs Arsenal scheduled
live: Chelsea vs Arsenal -> in_progress
live: start notification delivered to 12 subscribers
live: Chelsea vs Arsenal -> final
live: settled 2 picks
live: end notification delivered to 12 subscribers
```

The tracking count excludes terminal fixtures, including those with delivery
retries or unsupported picks left pending. Notification logs count successful
deliveries only. The logging adjustment passed all 29 live-tracking tests.

## Database and recovery

Startup's existing schema mechanism adds `live_fixture_state` using
`CREATE TABLE IF NOT EXISTS`, keyed by `(pick_date, fixture_key)`. It stores status,
first observed start/final times, last check, source, the complete result, and
settlement/notification completion flags. Prediction data stays in `picks`.

Final results are saved before grading. Pick settlement, Elo/result_seen changes,
calibration refitting, and the settlement completion flag commit together. A
restart can therefore finish grading from the saved result or retry only missing
deliveries without refetching or regrading completed work.

The additive migration test preserves existing picks, subscribers, deliveries,
prediction_cache, provider_cache, and key/value rows. No production database was
opened or modified during implementation; `/data/predbot.db` upgrades at startup.

## Notifications

Only active subscribers with recorded `picks_top` or `picks_daily` delivery for
the relevant publication date qualify. Each message includes only predictions
from cards that recipient received. Both tiers still produce one fixture alert.
Names and outcomes use existing display helpers; content is HTML escaped.

Sample start:

```text
🟢 Match Started

⚽ Football
🏆 English Premier League
Chelsea vs Arsenal

🎯 Chelsea to Win
```

Sample end:

```text
🏁 Match Ended

⚽ Football
🏆 English Premier League
Chelsea 2–1 Arsenal

✅ Chelsea to Win — WON
```

All existing outcomes are displayed: WON, LOST, PUSH, HALF WON, HALF LOST, VOID,
PENDING. Postponed/cancelled fixtures void through existing grading without a
false “Match Started” or “Match Ended”. Missing a start transition produces only
the end alert when final is confirmed.

Deduplication uses the existing `deliveries` table with
`match_start:<sha256(fixture_key)>` / `match_end:<sha256(fixture_key)>`, publication
date, and chat ID. Each successful send is recorded immediately. Failed sends
retry, including after restart and after the stale watch window expires.

Delivery boundary limitation: recorded successful sends are never repeated.
An absolute exactly-once guarantee is not possible if Telegram accepts a message
and the process dies before its SQLite delivery record commits, or the success
response is lost. Those two systems do not share a transaction; this narrow
window can cause a retry of an already received message. No second delivery
system or change to onboarding was introduced.

## Settlement and recap

`settle_result()` reuses the existing grader and persistence/learning logic with
a known result. It rechecks pending status under a SQLite write transaction.
Different home/away orientations of one fixture grade correctly and learn Elo
once. Stat-only enrichment retains the confirmed full score for learning.

The regression test confirms a football final at 15:04 for a 15:00 kickoff
settles immediately. At the same instant, ordinary `settle.run()` leaves that
pick pending without a result lookup because `DURATION_H` has not elapsed.
Ordinary and immediate settlement produce the same outcome, and an already
settled pick cannot be graded a second time.

`DynamicJob("settle", ...)` remains. `job_recap()` still runs settlement first;
`predbot/jobs/recap.py` and its schedule are unchanged. The next-day regression
reads the live-settled result through existing recap/stats logic, with no second
settlement or duplicate learning/result rows.

## Validation

Focused command:

```text
python -m pytest tests/test_live_results.py tests/test_results_espn.py tests/test_phase3_settlement.py tests/test_settle_parallel.py -q --tb=short
```

Result: **141 passed**. Coverage includes status parsing, repeated polls,
scheduled-to-final, duplicate tiers, recipient filtering, 100 subscribers,
restart before/after start/final, crash before settlement and before delivery,
partial delivery retry, stale/early watches, safe source fallback, scoreboard
reuse, paid API exclusion/enrichment, reversed teams, calibration/Elo,
unsupported stats, cancellation, migration, optional scheduling, and recap.

Full suite was run once with `python -m pytest -q --tb=short`:
**1,025 passed, 3 failed**. The final stat-enrichment refinement was then verified
with the focused command above.

All three failures reproduce in an isolated copy of committed baseline
`7ba1784`, without changing assertions or golden files:

- `tests/test_picker.py::test_top_finds_a_band_combo_that_only_high_priced_legs_can_make_in_a_300_leg_pool`
  exceeds its 2-second threshold: 2.67 seconds in the full run, 3.69 on baseline.
- `tests/test_replay_golden.py::test_replay_reproduces_the_golden_slate_byte_for_byte`
- `tests/test_replay_golden.py::test_replay_reproduces_the_golden_render_byte_for_byte`

Actual replay outputs on baseline and the working tree are byte-identical.
Their SHA-256 digests are:

```text
slate  49a6ff3f9d6c59c4c2b2cfb2e9e843bcf922d9b6e80b5b65840878becbab118c
render 1fab9f67645271075b6854e252e671e330a323db64485fac237670ec74639993
```

Tests used local fake providers and temporary SQLite databases. No real Telegram
messages or paid result requests were sent. `git diff --check` is clean.

## Final confirmations

| Requirement | Confirmed |
| --- | --- |
| ESPN scheduled no longer means in_progress | YES |
| Only stored published fixtures are monitored | YES |
| Kickoff alone never triggers Match Started | YES |
| One grouped fixture lookup per polling cycle | YES; source fallbacks as described above |
| Match Started sent once per eligible subscriber | YES for recorded successful deliveries; see delivery boundary limitation |
| Match Ended sent once per eligible subscriber | YES for recorded successful deliveries; see delivery boundary limitation |
| Confirmed final picks settle immediately | YES when required grading data is available |
| Repeated polling never regenerates predictions | YES |
| OddsAPI/SGO are not hammered for live status | YES |
| Existing DynamicJob settlement remains | YES |
| Existing next-day recap remains | YES |
| Restart does not duplicate recorded successful notifications | YES; absolute exactly-once delivery across the send/commit boundary is not guaranteed |
