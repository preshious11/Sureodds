> Historical investigation. The cache/retry implementation below has been superseded
> by the SQLite-only architecture documented in SQLITE_CACHE_REPORT.md.

# Prediction generation investigation — September 16, 2026

## Findings from actual provider GET requests

Credentials were available locally. Diagnostics used an in-memory copy of local
quota/model state, never wrote production picks/subscribers/cache, and never sent
Telegram messages. Requests consume provider allowance. The hosted justrunmy.app
database/cache and environment were not available for inspection.

The initial collection used the existing provider page limits. A follow-up used
one SharpAPI page per configured league and disabled API-Football prediction
feature requests to bound cost. These are diagnostic samples, not proof that
every available market was exhausted. Counts from different samples are not
directly comparable.

### Football root cause in the observed run

1. API-Football returned an application-level quota error on `/fixtures`, with
   zero response rows. This is **unavailable data**, not evidence of zero fixtures.
2. SportsGameOdds exposes eight leagues to this key. Its configured football
   leagues are UEFA_CHAMPIONS_LEAGUE and MLS; both returned zero events with odds
   in the remaining application-day window. EPL, La Liga, Serie A and the other
   competitions listed in the request are not configured SGO leagues here.
3. SharpAPI is configured for mlb, mls, nba, nfl, nhl. The first MLS page returned
   soccer rows, so the sport mapping works. A separate probe found 56 rows dated
   September 16, all `conference_winner`/`tournament_winner`, and 144 rows dated
   September 18. These futures are not supported match markets. The main-market
   probe returned September 18–19 match odds, outside today's window. Do not
   relabel these as today's football fixtures.

Thus no usable football match event reached scoring in the observed run.
This is primarily a quota/coverage/window finding, not proof that football
matches do not exist today. No football or league was fabricated or added.

### Top Picks root cause in the observed run

The follow-up baseball pool had 3,549 scored markets. Sequential Top gates left:

| Gate | Remaining candidates |
| --- | ---: |
| Configured Top odds range | 2,024 |
| Confidence >= configured 0.62 | 346 |
| EV >= configured 0.02 | 44 |
| Model agreement >= 0.50 | **0** |
| At least two probability sources; no props | 0 |

Top was empty because **no candidate met its quality gates**, before accumulator
selection. The old greedy selector did not cause this particular empty result:
it accepted its first eligible candidate. Its separate weakness was that it
could settle on a poorer accumulator without searching a better combination.
The agreement bar and multi-source requirement have not been weakened.

## Actual pipeline counts

The follow-up window was September 16, 11:27 UTC through 23:00 UTC (midnight WAT).

| Stage | Football | Baseball |
| --- | ---: | ---: |
| SGO raw events in configured league/window requests | 0 | 4 |
| Normalized usable merged events | 0 | 4 |
| Normalized/scored markets | 0 | 3,549 |
| Scored confidence >=50% | 0 | 1,619 |
| Scored confidence <50% | 0 | 1,930 |
| After lead-time/settleability/valid-price base filters | 0 | 3,264 |
| Base candidates >=50% | 0 | 1,334 |
| Daily eligible after configured quality filters and >=50% preference | 0 | 16 |
| Diagnostic selected Daily ticket | 0 | 4 |
| Top eligible | 0 | 0 |
| Top combinations checked | 0 | 0 |

The four-event SGO sample was collected before the additional cursor refill was
implemented. Final code reuses spare allowance from empty leagues to retrieve
additional pages without exceeding the existing object limit. No additional live
run was made merely to inflate these counts.

The initial, broader diagnostic returned eight merged baseball events, 7,786
scored candidates, 3,566 >=50%, three Daily selections and zero Top selections.
See the separate JSON artifacts; do not mix those numbers with the follow-up.
Neither historical diagnostic captured a separate candidate-dedupe count. New
generation logs include `unique_candidates_by_sport`; no count was invented.
Selection diagnostic calls intentionally bypass publication so rejected partial
provider data can still be inspected. Production now withholds that generation.

## Complete data path

API-Football `fixture.date` / SGO `status.startsAt` / SharpAPI `event_start_time`
-> `parse_iso` UTC datetime -> provider sport and league normalization
-> configured league restrictions and kickoff window -> internal MarketLine
-> ProviderChain event merge (sport + teams + nearby kickoff)
-> EventModel de-vigged market, score model, optional Elo/APIF probabilities
-> Scored candidates -> lead-time/settleability/finite-price gates
-> independent Top eligibility and bounded combination search
-> Daily eligibility (>=50% first; lower probabilities only if none qualify)
-> equivalence/conflict checks and existing diversification
-> persisted picks with UTC kickoff and local publication date
-> DailySnapshot top_picks/daily_picks/generation metadata
-> cache roundtrip -> shared Top/Daily renderer.

Top selection happens before Daily pagination and never consumes a rendered page.
Daily remains the full ticket made of Top plus additional Daily selections.

## Provider and configuration findings

- Enabled order: api_football, sportsgameodds, sharpapi. All three local keys exist.
  The Odds API has no active provider implementation here.
- API-Football: football fixtures, odds and optional prediction features. Its
  configured league allowlist is empty, meaning unrestricted; PRIORITY_LEAGUES
  ranks feature-request priority rather than excluding fixtures.
- SGO: fixtures and odds across its configured eight leagues. SOCCER correctly
  maps to football; FOOTBALL means American football. Parsing now accepts sport
  key casing consistently. Unsupported leagues were not added.
- SharpAPI: configured MLS is the soccer source; `soccer` maps to football, while
  `football` maps to American football. Futures are not match predictions.
- The original chain continued after an empty or failed primary response, but
  could stop after a large single-sport result met CHAIN_MIN_EVENTS. That did not
  trigger in the eight-event diagnostic, but is a real coverage defect. All
  configured enabled providers are now attempted within their existing budgets.
- The original single SGO multi-league page could be filled by one league. The
  new collection probes every configured accessible league, shares the existing
  total object allowance, and spends unused allowance on cursor pages. This is
  acquisition budgeting, not a quota forcing sports into selected picks.
- Explicit API page/object limits still bound coverage. They are reported and
  are not a promise of exhaustive worldwide odds access.

Provider contract checks used official documentation:
[SharpAPI odds](https://docs.sharpapi.io/en/api-reference/odds/) and
[SGO cursor pagination](https://sportsgameodds.com/docs/guides/data-batches).

## Code fixes and cache behavior

The old code could catch a football failure, select baseball, persist it, and
cache it until UTC midnight. Simply clearing Redis then rebuilt the same picks
from SQLite. There was no failure/completeness metadata.

New generations record provider completion/errors, counts and attempt times in
SQLite KV and snapshot `generation` metadata. A failed/incomplete collection is
**withheld from publication and settlement storage**. The cached response is
explicitly partial and tells users that data is incomplete, rather than saying
no selection passed quality checks. It expires after at most 15 minutes.
Subsequent snapshot requests can retry, with a persisted maximum of three
attempts per application date. Successful provider responses are reused within
the process. After a restart, provider budgets still apply, but the in-memory
successful-response cache is lost. After three failures, recovery waits until
the next date or a deliberate administrative intervention.

This is intentionally conservative: an unavailable configured provider can
delay the whole new ticket. It does not secretly publish the available sport as
complete. Retries are demand-driven, not a new scheduler job; a later request can
recover publication after the initial scheduled build failed. Existing published
cards are not rewritten or removed automatically.

Cache keys, lookup date and TTL now follow the configured application day. Event
collection runs from now plus the existing lead time through local midnight
(bounded by PICK_WINDOW_HOURS). It no longer admits tomorrow just because it is
within the next rolling 24 hours, or prefers yesterday's UTC rows at WAT midnight.
UTC remains the internal timestamp representation.

The build lock was extended for multi-minute provider requests, and a reader
that times out waiting for another builder no longer starts a competing build.
Existing legacy snapshots have no new provenance; they cannot be retroactively
certified complete. Hosted cache contents were not inspected. The two local
databases contain September 15 selections (including football), not the reported
hosted September 16 baseball-only cache.

## Selection behavior and example

Daily keeps the existing odds, EV, agreement, settleability and diversity rules.
It now uses eligible >=50% candidates first; below-50% candidates are considered
only if no eligible preferred candidate exists. The configured fallback minimum
of 0.30 was not lowered. No arbitrary minimum number of football picks was added.
Consequently football can still lose legitimately on quality/ranking/constraints;
there is no dishonest guarantee that every sport appears every day.

Top search keeps its original quality gates, deduplicates match/price options,
bounds the search to 60 options, and evaluates 1–3 unique-match combinations.
It prefers the target to target+5% band, then the closest allowed above-target
ticket, then an under-target fallback. The existing 1.7x overshoot guard remains.
Ties use strength and outcome diversity. It is deterministic, not an exhaustive
optimizer over every raw provider market.

Regression example (synthetic test candidates, not betting recommendations):

| Selection | Confidence | Odds |
| --- | ---: | ---: |
| Match 2 home outcome | 75% | 1.38 |
| Match 3 home outcome | 73% | 1.50 |

Combined odds: **2.07**. The search finds this pair instead of stopping at the
first 2.26 candidate. Tests also cover three picks, single picks, unique matches,
maximum size three and serialized Top preservation.

## Files changed

- `predbot/providers/chain.py`: all-provider coverage, safe status/count logs,
  successful-provider reuse for retries.
- `predbot/providers/api_football.py`: partial-fetch status and fixture/league/odds counts.
- `predbot/providers/sportsgameodds.py`: league coverage, bounded cursor refill,
  partial failure status, normalized sport casing and local-window check.
- `predbot/providers/sharpapi.py`: internal failure status and normalization/page diagnostics.
- `predbot/selection/picker.py`: bounded Top combination search, >=50% Daily
  preference, finite-price validation and selection diagnostics.
- `predbot/jobs/daily_picks.py`: local-day window, persisted bounded retry state,
  stage counts and incomplete-publication guard.
- `predbot/cache/models.py`: backward-compatible generation metadata.
- `predbot/cache/fetcher.py`: local-day cache/TTL, partial response/retry handling,
  no partial pick persistence and no competing build after lock wait timeout.
- `predbot/bot/pagination.py`: truthful temporary-incomplete message only;
  prediction card layout and routing unchanged.
- `diagnose_predictions.py`: reproducible read-only diagnostic using isolated DB state.
- `tests/test_generation_regressions.py`: focused generation regressions.
- `prediction_diagnostic_before.json`, `prediction_diagnostic.json`,
  `sharp_football_diagnostic.json`: sanitized real-data evidence.
- `PREDICTION_GENERATION_REPORT.md`: this report.

Welcome, subscriptions, polling ownership, stats, grading, settlement, API keys
and current **09:00 WAT** broadcast timing were not changed. The pasted request
mentions 09:30, but the latest explicit scheduling instruction was 09:00 WAT.

## Validation and deployment

Focused suite: 46 passed. Full suite: 219 passed; no failures or new regressions.

From the nested application directory:

```text
python -m pytest -q tests/test_generation_regressions.py tests/test_picker.py tests/test_quota_handling.py tests/test_parsers.py -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python diagnose_predictions.py
```

The diagnostic uses real API GETs/quota; do not run it repeatedly unnecessarily.

Deploy/rebuild the updated code on justrunmy.app. API-Football access/quota must
recover before a complete generation can be expected with all providers enabled.
Do **not** delete the database, subscribers or settled picks. Do not clear today's
Redis key expecting new odds: persisted published picks take precedence.

No live cache was cleared and no live ticket was regenerated. Existing published
legacy tickets are preserved. The safest action is let the next application day
generate after quota recovery, and inspect `Prediction generation` logs for
provider states and football/Top eligibility. Rebuilding an already published
day needs a deliberate migration plan for its picks, deliveries and settlement;
the existing force-publish command is not a safe substitute for that review.

The repository defects are repaired and tested. The external football-data
availability and hosted deployment still require verification. An empty Top
list remains correct when the provider/model candidates fail its quality gates.
