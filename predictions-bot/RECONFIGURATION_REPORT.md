# RECONFIGURATION_REPORT

Reconfiguration of SureOdds / predbot for Nigerian SportyBet punters. Seven phase commits (Phase 0 - 6) plus a final-verification commit, all local, **not pushed**.

| | Tests |
|---|---|
| Baseline (start of Phase 0) | **471 passed** |
| After Phase 0 | 468 (three deleted scratch scripts were three cases of the parametrized `test_read_only_diagnostics`) |
| After Phase 1 | 457 (tests of the removed API-Football code deleted, listed below) |
| **Final** | **599 passed** (`python -m pytest -q`, from `predictions-bot/`) |

`python -c "import predbot.jobs.settle, predbot.providers.chain, predbot.providers.football_data"` succeeds.
`grep -rni "api_football\|apif\|api-football" --include=*.py --include=*.md --include=*.example .` returns only this report (it has to name the removed variables) and
`PREDICTION_GENERATION_REPORT.md` (the historical note at its top, plus the report body it describes, which was
deliberately not rewritten).

## Read this first - what you must do before deploying

1. **Get a free `FOOTBALL_DATA_KEY` (football-data.org) and set it.** Without it there is no football results
   path for Europe: in the live dry run below **every** EPL / La Liga / Serie A / Bundesliga / Ligue 1 / Championship /
   Eredivisie / Primeira / Super Lig candidate was rejected `NO_RESULTS_PATH`, so only MLS / MLB / NCAAF picks
   were possible. (Nigeria Premier League has no results source at all - see "Things I could not verify".)
2. **Tennis is effectively off** until a results path exists (ATP/WTA candidates: 26 events rejected `NO_RESULTS_PATH`).
   The Odds API tennis keys are excluded from `ODDS_API_LEAGUE_ALLOWLIST` for the same reason.
3. **The Odds API key is out of quota**: every call returns `x-requests-remaining=0, x-requests-used=500`. The bot
   correctly spends nothing (the 80-credit reserve gate stopped it), but the Top ticket gets no Odds API events until the
   monthly reset. I could not verify the `/scores` endpoint live for that reason.
4. Redeploy (a restart is not enough) and update the dashboard variables (block at the bottom).

## What changed, per phase

**Phase 0 (`ffe6eb5`).** Deleted the 14 committed scratch files and the truncated root `diagnose_providers.py`; root `README.md`
rewritten as UTF-8 with one pointer paragraph; `.gitignore` (both) gets `_*.txt`, `_*.py`, `*_diagnostic*.json`
(plus `!__init__.py`, because `_*.py` would otherwise also ignore every `__init__.py` a new package needs). Note `_top_fb_evidence.py`
(your untracked file) is now ignored by that pattern; it also still references the removed `cfg.chain_min_events`.

**Phase 1 (`59d1c0a`) - API-Football removed.** Deleted `providers/api_football.py`, `model/features.py`,
`tests/test_api_football_predictions.py`, `tests/fixtures/apif_*.json`. Ensemble sources are now market / score_model / elo;
`INDEPENDENT_SIGNALS={"elo"}`; settle no longer has the batch/statistics blocks; config keys, chain, `expected_sports`, docs,
`.env.example`, `SQLITE_CACHE_REPORT.md` and all diagnostics scripts updated. `_api_usage` works without `daily_limit`.
Tests deleted because the feature is gone: the four `apif` parser tests, the apif quota test, two raw-request cache tests,
the `api_football_configured_league_filter` test. All other tests were re-pointed at sharpapi/sportsgameodds/oddsapi.
`trace_pipeline.py` crashes on `_load_provider_cache` - **pre-existing** (the function was defined but never bound),
not fixed. The diagnostics scripts do not implement `--help`, so my first check ran them live (a few SharpAPI calls) - my mistake;
the rest of the verification blanked the keys.

**Phase 2 (`c4ffffd`).**
- 2a `PROVIDER_ORDER=oddsapi,sharpapi,sportsgameodds`; unknown names warn; SharpAPI key required (`main.py`, `cli.py` log
  `SHARPAPI_KEY is required — get one at https://sharpapi.io` and exit 2); the other keys warn.
- 2b Odds API is a full chain member (no second chain when the priority provider is already in the main chain -
  it is still passed to `select()`), `RESERVED_FOR_TOP_PRIORITY_SOURCE` deleted, 9 credits/cycle, 80 reserve,
  `ODDS_API_LEAGUE_ALLOWLIST` (11 keys) applied in `_discover_sports`. Monthly maths is in `.env.example` (~270 generation + ~180 scores ≈ 450 / 500).
- 2c `ALLOWED_SPORTS` enforced in `chain.filter_allowed` before merging (also on the persisted-cache path), counted as `sport_rejected`
  in the provider report; hockey/handball removed from every table and period ids `1p/2p/3p` removed.
- 2d SharpAPI: explicit 19 leagues, `auto` removed (warning), `SHARPAPI_MAX_PAGES=3`; `sport_for_league` maps every default slug
  (guarded by `NON_SOCCER_PREFIXES` so the new `league`/`liga` hints cannot capture table tennis etc.).
- 2e SGO: empty `SGO_SPORTS` no longer filters every league out (the regression test fails without the fix - verified);
  `SGO_ALLOWED_STATS` / `SGO_ALLOWED_PERIODS`; `MAX_PLAYER_PROPS=0` (this key already existed for the picker's cap, default 2; one setting now drives both).
- 2f `DISABLED_MARKETS=odd_even,prop,winning_margin,ht_ft` gives a `DISABLED_MARKET` rejection.

**Phase 3 (`404d4e9`).** `providers/football_data.py` (results only; header auth; 6.5 s limiter; per-run `(competition,date)` cache;
90-minute score for extra-time matches - v4 `fullTime` includes extra time/penalties, so `regularTime` is used and an
unusable result is `unknown`, never guessed); `OddsApiProvider.fetch_result` via `/scores?daysFrom=3`; `results_path()` +
`make_results_gate()` in `markets/settlement.py`; `ResultsResolver` order origin, alt ids, football-data, SGO with every call wrapped;
`settle.run` never raises per pick; `CommandLoop._poll` wraps each update; `job_recap` wraps `job_settle`. The stored pick now carries
`_sport_key` and `_league_id` in `sources`.

**Phase 4 (`e89fc53`).** `retry_partial` job; regeneration replaces a day's pending picks when nothing was delivered; `DynamicJob`
keyed on the deadline instant (and it re-derives the deadline after a run, otherwise the next day's would never be computed);
`DELIVERY_TIME` (one shared instant in `TIMEZONE`); user/delivery reads take `prediction_lock` non-blocking; scheduler `loop` wraps
`due()`/`run()`; settlement deadline skips unparseable `start_utc`.

**Phase 5 (`4cbe68c`).** `PRICE_MODE` median/best; `Odds: 2.10 (median of 6 books)` / `Odds: 2.35 (Bet365)`; stat words through
`readable_stat`; `pretty_league()` (SharpAPI events now carry `England • Premier League`); `odds as of <time>` next to each Combined Odds line
(`Generated …` kept). No golden test needed changing except the sharp best-price parser test (now `price_mode="best"`).

**Phase 6 (`8407892`).** Locale detection (`en-US`, `pt-br`, bare `en` to Africa/Lagos); `/timezone Area/City` is back (canonical spelling,
listed in `/help`; the test that asserted it was gone now asserts it exists); calibration trains on `prob_raw` (additive migration, `Scored.p_raw`,
fallback to `prob` for old rows); `.env` inline-comment rule; removed `CHAIN_MIN_EVENTS`, `Database.recent_pick_keys`, `Config.enabled_providers`;
`.env.example` now matches every code default (checked programmatically); README provider/settlement/delivery/command sections rewritten.

## Decisions I made (where the brief left room, or I deviated)

1. `picker._settleable` was **kept** (it is the market-level "can this market be graded" check) and the event-level `results_path` gate was
   added beside it, rather than replacing it. The gate only runs when the chain exposes its providers (a real `ProviderChain`).
2. The 2b "already in the main chain" test uses the chain actually passed to `build_picks` (`chain.order`), which equals `cfg.provider_order` in production.
3. Odds API scores: one `/sports/{key}/scores?daysFrom=3` call per key per run (2 credits), matched by event id, instead of one call per `eventIds=`.
   Coverage is assumed for `soccer_*` keys only (`ODDS_API_SCORES_PREFIXES`); **unverified live** (quota 0).
4. football-data lookups without a known competition fall back to one `/matches` call (all competitions on the plan). The v4 payload shape in
   `tests/fixtures/football_data_matches.json` is built from the documented shape; **never called live** (no key here).
5. `ProviderChain(providers, order, min_events=None)` - the third argument is now optional and ignored so ~40 test call sites did not have to change; all production call sites dropped it.
6. Phase 4e: when the lock is busy a user read still returns the **already-published** card if one exists; only a read with nothing to serve gets the "being generated" placeholder.
7. Phase 4b: when the previous picks are kept because a regeneration failed catastrophically, their rows are kept too (`replace_pending=False`).
8. `SGO_EVENT_LIMIT`: code default 200 to 50, example 25 to 50.
9. SGO `find_result` coverage in `results_path` uses the leagues SGO reported available (or, before that is known, the configured ones), with a small SharpAPI-slug to SGO-id table.

## Env vars

**Added:** `FOOTBALL_DATA_KEY`, `ODDS_API_LEAGUE_ALLOWLIST`, `ALLOWED_SPORTS`, `DISABLED_MARKETS`, `SGO_ALLOWED_STATS`, `SGO_ALLOWED_PERIODS`, `DELIVERY_TIME`, `PRICE_MODE`.
**Removed:** `API_FOOTBALL_KEY`, `API_FOOTBALL_HOST`, `APIF_LEAGUE_IDS`, `APIF_DAILY_LIMIT`, `APIF_PREDICTIONS_PER_RUN`, `APIF_ODDS_PAGES`, `CHAIN_MIN_EVENTS`. **Renamed:** none.

| Variable | Old default | New default |
|---|---|---|
| `PROVIDER_ORDER` | `api_football,sportsgameodds,sharpapi` | `oddsapi,sharpapi,sportsgameodds` |
| `ODDSAPI_MAX_CREDITS_PER_CYCLE` | 15 | 9 |
| `ODDSAPI_RESERVE_CREDITS` | 50 | 80 |
| `SGO_LEAGUES` | `UEFA_CHAMPIONS_LEAGUE,MLS,NBA,NCAAB,NHL` | `NBA,NFL,MLB,MLS,NCAAF,NCAAB,UEFA_CHAMPIONS_LEAGUE` |
| `SGO_EVENT_LIMIT` | 200 (example 25) | 50 |
| `SHARPAPI_SPORTS` | `auto` | *(empty)* |
| `SHARPAPI_LEAGUES` | *(empty)* | the 19 slugs in `.env.example` |
| `SHARPAPI_MAX_PAGES` | 12 | 3 |
| `MAX_PLAYER_PROPS` | 2 | 0 |

Unchanged but relevant: `POST_TIME=09:00`, `TIMEZONE=Africa/Lagos`, `ODDS_API_REGIONS=eu`, `TOP_PROVIDER_PRIORITY=oddsapi`.

## Final verification: `python cli.py picks --dry-run`

Run live through `cli.main(["cli.py","picks","--dry-run"])` with a **clean env file** holding only the provider keys (your local
`.env` still carries stale overrides - see below), DB in a temp dir, nothing posted, no Telegram token. Cost: SharpAPI 34 requests, SGO 37 objects,
Odds API 0 credits. Took about 4 minutes (SharpAPI is spaced 5.5 s per call). Provider collection order in the log: **oddsapi, sharpapi, sportsgameodds**.

Odds API: `x-requests-remaining=0` then `quota/reserve gate stopped the odds fetch` (0 credits spent; 11 in-season allowlisted keys, 9 with events, 3 chosen at <= 9 credits).
SGO rejections: `UNSUPPORTED_STAT 24256, UNSUPPORTED_PERIOD 2416, PLAYER_PROP_LIMIT 2203, PROP_UNSETTLEABLE 914` - no inning/quarter/player explosion reaches the model.

Selection diagnostics (excerpt):

```
'base_by_sport': {'baseball': 688, 'football': 1140, 'american_football': 266}
'selection_rejections': {'NO_RESULTS_PATH': 1158, 'INVALID_PRICE': 88, 'DISABLED_MARKET': 774}
'no_results_path_by_league': {'england_-_premier_league': 2, 'spain_-_la_liga': 3, 'italy_-_serie_a': 3,
   'germany_-_bundesliga': 3, 'france_-_ligue_1': 5, 'england_-_championship': 1, 'netherlands_-_eredivisie': 3,
   'portugal_-_primeira_liga': 3, 'turkey_-_super_lig': 3, 'atp': 18, 'wta': 8}
picker: pool=4114 eligible=2094 top_pool=178 daily_pool=781 -> top=2 daily=7 ticket=18.59
```

No hockey, no odd/even (`DISABLED_MARKET` = 774), no underscores in any label, bookmaker info on the cards. Cards as generated:

```
📊 TODAY'S RESULTS

🔥 TOP PICKS
Combined Odds: 2.00 · odds as of 12:07 PM WAT
Total Picks: 2
Baseball
MLB
Baltimore Orioles vs Milwaukee Brewers
<b>Prediction:</b> Baltimore Orioles +2.5
Odds: 1.34 (Fanduel)
Confidence level: <b>71%</b> | 9:05 PM WAT

Football
MLS
DC United vs Charlotte FC
<b>Prediction:</b> DC United or Draw (1st Half)
Odds: 1.49
Confidence level: <b>66%</b> | 12:30 AM WAT


📋 DAILY PICKS
Combined Odds: 9.28 · odds as of 12:07 PM WAT
Total Picks: 7
Football
MLS
Colorado Rapids vs Seattle Sounders FC
<b>Prediction:</b> BTTS No (1st Half)
Odds: 1.22 (consensus of 3 books)
Confidence level: <b>77%</b> | 2:30 AM WAT

Football
MLS
New England Revolution vs Orlando City SC
<b>Prediction:</b> New England Revolution or Orlando City SC
Odds: 1.25
Confidence level: <b>77%</b> | 12:30 AM WAT

Baseball
MLB
Cleveland Guardians vs Athletics
<b>Prediction:</b> Cleveland Guardians to Win
Odds: 1.54 (consensus of 9 books)
Confidence level: <b>62%</b> | 11:10 PM WAT

Baseball
MLB
Tampa Bay Rays vs BOS Red Sox
<b>Prediction:</b> Over 5 Total Runs
Odds: 1.22 (Draftkings)
Confidence level: <b>79%</b> | 9:10 PM WAT

Football
MLS
FC Dallas vs Austin FC
<b>Prediction:</b> FC Dallas Over 1.5 Goals
Odds: 1.53 (consensus of 3 books)
Confidence level: <b>61%</b> | 1:30 AM WAT

Baseball
MLB
Chicago White Sox vs Detroit Tigers
<b>Prediction:</b> Detroit Tigers +2.5
Odds: 1.29 (Fanduel)
Confidence level: <b>74%</b> | 7:10 PM WAT

American Football
NCAAF
Columbia vs Lafayette
<b>Prediction:</b> Columbia to Win
Odds: 1.64 (consensus of 3 books)
Confidence level: <b>58%</b> | 5:00 PM WAT

<i>Generated 12:07 PM WAT</i>
```

Notes on the output: single-book lines show that book (`Fanduel`, `Draftkings`) and SportsGameOdds lines say `consensus of N books`; the
`median of N books` wording appears when two or more books are merged (covered by tests; not present in this slate because The Odds API returned nothing).

## Things I could not verify

- football-data.org live (no key): payload shape, the `/matches` no-competition fallback, rate behaviour.
- The Odds API `/scores` coverage per sport key and its 2-credit cost (key at 0/500).
- That production has a `/data` volume, and which dashboard variables are currently set.
- SharpAPI league slugs beyond the ones that returned events today (`nigeria_-_premier_league`, Europa/Conference League and Euroleague returned nothing in the window).
  Nigeria Premier League also has no results source in any configured provider, so it would be `NO_RESULTS_PATH` even if fixtures appeared.

## Also noticed (not changed)

- Odds API and SharpAPI events for the same fixture do not merge in `merge_events`: the league strings differ (`EPL` vs `England • Premier League`) and
  `ex.league != ev.league` blocks the match. Pre-existing; the per-fixture cap in the picker still stops duplicates, but "backed by several books" only happens within a provider.
- Your local `predictions-bot/.env` still has stale overrides (`PROVIDER_ORDER=api_football,…`, `MAX_PLAYER_PROPS=2`, `ODDSAPI_*` 15/50, `SGO_LEAGUES` with NHL, `SGO_EVENT_LIMIT=25`, the removed `APIF_*`/`API_FOOTBALL_*`/`CHAIN_MIN_EVENTS`). The startup report lists them; I did not edit the file.

## Dashboard variables (justrunmy.app)

Set (name=value):

```
SHARPAPI_KEY=<your key>
ODDS_API_KEY=<your key>
SPORTSGAMEODDS_KEY=<your key>
FOOTBALL_DATA_KEY=<new - free at football-data.org>
PROVIDER_ORDER=oddsapi,sharpapi,sportsgameodds
DELIVERY_TIME=09:30
TIMEZONE=Africa/Lagos
POST_TIME=09:00
```

Delete (so the new code defaults apply): `API_FOOTBALL_KEY`, `API_FOOTBALL_HOST`, `APIF_LEAGUE_IDS`, `APIF_DAILY_LIMIT`, `APIF_PREDICTIONS_PER_RUN`, `APIF_ODDS_PAGES`,
`CHAIN_MIN_EVENTS`, and any of `ODDSAPI_MAX_CREDITS_PER_CYCLE`, `ODDSAPI_RESERVE_CREDITS`, `SGO_LEAGUES`, `SGO_EVENT_LIMIT`, `SHARPAPI_SPORTS`, `SHARPAPI_LEAGUES`,
`SHARPAPI_MAX_PAGES`, `MAX_PLAYER_PROPS` that still pin an old value.
