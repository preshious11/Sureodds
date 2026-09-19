# Results coverage — report

Follows `RECONFIGURATION_REPORT.md` and `SPORT_COVERAGE_AUDIT.md`. Suite: **704 passed** (was 608 at the start of this work). Nothing is pushed. Keys are never printed here (presence only).

## What changed, per phase

| Commit | Phase | Change |
|---|---|---|
| `7e8e516` | **A** ESPN, **B** EuroLeague, **C** gate rework (one commit — see "Deviations") | `providers/espn.py`, `providers/euroleague.py`, `markets/competitions.py`, gate/resolver/picker/config/`/lastrun`, tennis grading, fixtures and tests |
| `bc80867` | **D** card | `Odds:` line is the plain price; `odds_note` removed from `format.py`; the price source (median of N books / bookmaker) is listed per pick in `/lastrun` under "Price sources" |
| `dd77c3a` | **E** SGO status | `sport_status` keyed by canonical sport name; the event counter now matches, so `success` is reachable |
| `5c15645` | ESPN user agent | ESPN's edge returns 403 to every user agent I tried except python-requests' default; the provider sends that one |
| `(docs commit)` | docs | README, this report; a test for sets-market grading |
| — | **F** TheSportsDB | **Skipped**: ESPN has a Nigeria slug (`soccer/nga.1`, "Nigerian Professional League", 10 events on a Sept day), so nothing is left without a path |

### A — ESPN (`predbot/providers/espn.py`)
Results-only, keyless, `RateLimiter(1.0)`. Parsing decisions from the recorded payloads (`tests/fixtures/espn_*.json`):
- **Soccer** final status is `STATUS_FULL_TIME` (not `STATUS_FINAL`), and the scoreboard has **no linescores**, so soccer gives `full` only (no 1h/2h). `STATUS_FINAL_AET/PEN` without period scores → `unknown` (the score would include extra time); the resolver then falls through.
- **Basketball / NFL / NCAAF**: `full` including overtime; quarters `1q..4q`, `1h`, and `2h = full − 1h` (NCAAB has halves only). **MLB**: `full` only.
- **Tennis**: an event is a tournament; matches are in `groupings[].competitions[]` (singles only). `full` = sets won, `stats[("games","full")]` = total games, `("sets","full")` too. `STATUS_RETIRED` / `STATUS_WALKOVER` / `STATUS_CANCELED` → `cancelled`; scheduled/in progress → `in_progress`.
- Matching: kickoff ±3 h and `same_team()` on displayName / shortDisplayName / abbreviation (athlete names for tennis); the caller's home/away may be the other way round (the returned score follows the caller's order).
- Requests: **one date per request**. The brief's `dates=YYYYMMDD-YYYYMMDD` range returned **400** for the team-sport leagues (it worked for tennis only), so the provider queries the kick-off's UTC day and, if the match is not there, the day before (ESPN groups by US Eastern date, never later than UTC). Cached per (league, day) for a settlement run.
- `groups`: college basketball returned 2 events without `groups=50` and 46 with it → sent. College football returned the same 80 events with and without `groups=80`; it is sent anyway (harmless).

### B — EuroLeague (`predbot/providers/euroleague.py`)
`GET https://api-live.euroleague.net/v2/competitions/E/seasons/{E2026}/games?limit=500` (JSON, keyless). One call returns the whole season (402 games), cached per run. Fields: `played`, `utcDate`, `local|road.club.{name,abbreviatedName,editorialName,code}`, `score` (includes overtime), `partials1..4`, `extraPeriods`. Season code = year of the July start. Full + `1q..4q` + `1h` + `2h` (2h includes overtime). The unplayed 2026-27 game in the fixture has scores of 0 and `played=false` → `in_progress`, never a 0-0 final.

### C — gate, resolver, table, tennis
- **`markets/competitions.py`**: one row per competition with `sharp`, `oddsapi`, `sgo`, `football_data`, `espn`, `euroleague` ids. `SHARP_TO_SGO_LEAGUE`, `ODDS_API_SCORES_PREFIXES` and the football-data map are gone (football-data's `competition_for` now reads the table). `competition_for_event()` resolves from `extra["sport_key"]`, `league_id`, then the display league.
- **`settlement.result_paths(event, cfg, providers)`** returns the ordered `ResultPath` list (origin SGO → football_data → espn → euroleague → oddsapi `/scores` → SGO `find_result`), each with the periods/stats it can grade. `results_path()` remains as "name of the first path or None". `make_results_gate` returns the list.
- **Picker**: after the `NO_RESULTS_PATH` check, a market whose period no path grades is rejected `PERIOD_UNSETTLEABLE`; one whose stat no path grades (corners/cards on a score-only path) is rejected `STAT_UNSETTLEABLE` (extra, same rule).
- **`ResultsResolver`** walks the same list; first non-`unknown` result wins; every call wrapped in `except ProviderError`; the Europa/Süper Lig special case is removed. A stored pick whose competition cannot be identified (old rows) still gets a best-effort football-data / SGO lookup.
- **Do not fetch what cannot be settled**: `build_providers` marks each fixture provider's `skipped_no_results`; `SharpApiProvider._targets()`, the SGO league list and the Odds API discovery skip those, one `log.warning` per league. If *every* league is skipped nothing is fetched (it does not fall back to the unfiltered feed). `/lastrun` shows "Leagues not fetched (no results path)", `NO_RESULTS_PATH` / `PERIOD_UNSETTLEABLE` / `STAT_UNSETTLEABLE` counts, and events per league without a path.
- **Tennis**: `atp,wta` back in `SHARPAPI_LEAGUES` (19 leagues). `result2` grades on sets; `total` / `handicap` grade on games (`stat` `games`; a bare `points` stat on a tennis total is treated as games) and on sets when the stat is `sets`.
- **Startup**: one table of every configured league → path(s), `NONE` logged as a warning (below, from the dry run).

## Environment variables

| Variable | Change |
|---|---|
| `ESPN_RESULTS` | **new**, default `1` |
| `EUROLEAGUE_RESULTS` | **new**, default `1` |
| `SHARPAPI_LEAGUES` | default now includes `atp,wta` (19 leagues). **A dashboard value overrides this** — if one is set, add `atp,wta` there too. |
| `FOOTBALL_DATA_KEY` | unchanged (set in the local `.env`; `.env.example` keeps it empty) |

## League → results path, as it stands
Providers on: football-data (key set), ESPN, EuroLeague, SGO, Odds API.

| Origin | League id | Path(s), in order |
|---|---|---|
| sharpapi | `england_-_premier_league` | football_data → espn |
| sharpapi | `spain_-_la_liga` | football_data → espn |
| sharpapi | `italy_-_serie_a` | football_data → espn |
| sharpapi | `germany_-_bundesliga` | football_data → espn |
| sharpapi | `france_-_ligue_1` | football_data → espn |
| sharpapi | `england_-_championship` | football_data → espn |
| sharpapi | `netherlands_-_eredivisie` | football_data → espn |
| sharpapi | `portugal_-_primeira_liga` | football_data → espn |
| sharpapi | `turkey_-_super_lig` | espn |
| sharpapi | `uefa_-_champions_league` | football_data → espn → sportsgameodds |
| sharpapi | `uefa_-_europa_league` | espn |
| sharpapi | `uefa_-_europa_conference_league` | espn |
| sharpapi | `nigeria_-_premier_league` | espn |
| sharpapi | `atp` | espn |
| sharpapi | `wta` | espn |
| sharpapi | `nba` | espn → sportsgameodds |
| sharpapi | `nfl` | espn → sportsgameodds |
| sharpapi | `mlb` | espn → sportsgameodds |
| sharpapi | `euroleague` | euroleague |
| oddsapi | `soccer_epl`, `soccer_spain_la_liga`, `soccer_italy_serie_a`, `soccer_germany_bundesliga`, `soccer_france_ligue_one`, `soccer_efl_champ`, `soccer_netherlands_eredivisie`, `soccer_portugal_primeira_liga` | football_data → espn → oddsapi |
| oddsapi | `soccer_uefa_champs_league` | football_data → espn → oddsapi → sportsgameodds |
| oddsapi | `soccer_uefa_europa_league`, `soccer_turkey_super_league` | espn → oddsapi |
| sportsgameodds | `NBA`, `NFL`, `MLB`, `MLS`, `NCAAF`, `NCAAB` | sportsgameodds → espn |
| sportsgameodds | `UEFA_CHAMPIONS_LEAGUE` | sportsgameodds → football_data → espn |

No configured league is `NONE`. Periods graded: football_data `full/1h/2h`; ESPN `full` (soccer, MLB, tennis) or `full/1h/2h/1q..4q` (NBA, NCAAB, NFL, NCAAF); EuroLeague `full/1h/2h/1q..4q`; oddsapi `/scores` `full`; SGO unrestricted.

## Verified live, and what could not be

**Verified live (free calls):**
- ESPN: scoreboard responses recorded for eng.1, uefa.europa, tur.1, usa.1, nba, mens-college-basketball, nfl, college-football, mlb, atp, wta; `nga.1` and `uefa.europa.conf` exist and return events.
- EuroLeague v2 API: recorded 2025-26 games (played, overtime) and one 2026-27 unplayed game.
- `ResultsResolver.resolve` against the live services (football-data, ESPN, EuroLeague only; no Odds API / SGO), for last week's fixtures:

| Case | Answered by | Result |
|---|---|---|
| Premier League, Bournemouth v Brentford (2026-09-12) | **football_data** | final 2-2, 1h 1-1, 2h 1-1 |
| Europa League, Levski Sofia v RB Salzburg (2026-09-17) | **espn** | final 0-1 |
| Süper Lig, Eyüpspor v Çaykur Rizespor (2026-09-12) | **espn** | final 0-2 |
| WTA, Panova v Scott (2026-09-12) | **espn** | sets 0-2, games 6-12 |
| ATP, Pavlovic v Varillas (2026-08-24) | **espn** | sets 2-1, games 19-15 |
| Euroleague, Olympiacos v Real Madrid (2026-05-24, **last season**) | **euroleague** | final 92-85, quarters and halves |

  football-data.org was called **once** in that run (`/competitions/PL/matches`); with the first attempt (when ESPN was still blocked by its user agent) that is the two live calls allowed.
- `cli.py picks --dry-run` (one run, local `.env`, temp DB, nothing posted). **Deviation:** I blanked `SPORTSGAMEODDS_KEY` and `ODDS_API_KEY` for this run (brief: zero live calls to those two), so it spent SharpAPI only (37 requests). Startup coverage table: see the league table above (every line had a path). Then: `no_results_path_by_league: {}`; `selection_rejections: {'STAT_UNSETTLEABLE': 261, 'INVALID_PRICE': 93, 'PERIOD_UNSETTLEABLE': 79}`; picker `pool=1126 eligible=693 top=2 daily=7`; Top 2.01, Daily 9.11. Top: Sevilla v Barcelona Over 2.5 (1.30), Orioles v Brewers +1.5 (1.55). Daily: Ligue 1, Eredivisie ×2, MLB, **WTA ×2**, Premier League; one tennis pick was a sets handicap. Every `Odds:` line is the plain price. What the two rejection counters are: the candidates in the run that would previously have been accepted and left pending — football corners/cards lines (`total/corners` 196 + `handicap/corners` 32 + `total/corners` 1h 31 + `total/cards` 18 in the cached SharpAPI events) for `STAT_UNSETTLEABLE`, and first-5-innings (42), tennis first-set (37) and similar lines for `PERIOD_UNSETTLEABLE` (counts from the cached events, before the other filters).

**Not verified / unverifiable:**
- **The SGO status block on a live run** (zero SGO calls allowed). It is covered by a regression test that parses the recorded SGO event (`sport_status["football"] == {"status": "success", "events": 1, "leagues": 1}`, no `SOCCER`/`FOOTBALL` key). The brief's example spells the key `"state"`; the existing field is `"status"` and other code reads it, so I kept it.
- **Euroleague 2026-27** has not started (the API lists its games as unplayed), so the live check used a 2025-26 game.
- **Conference League** and **Nigeria**: the slugs exist, but no completed match was available to resolve (the Conference League scoreboard had 0 events on 2026-09-17 and 18 scheduled on the 19th; Nigeria 0 on 2026-09-13). The path is covered by the fixture-free table test only.
- **ESPN extra-time/penalty soccer results** were never seen live; the code returns `unknown` when it cannot isolate the 90 minutes.
- **ESPN in production**: from this machine ESPN answers 403 to every user agent except python-requests' default (`predbot/2.0`, two browser-style agents were refused). That is bot-filter behaviour and may differ from the justrunmy.app host's IP; if ESPN blocks it, the `ProviderError` is swallowed and settlement falls through to the next path (for a league with only ESPN, the pick stays pending). Check the first settle log after deploying.
- **Odds API `/scores`** and SGO settlement: zero live calls, as instructed.

## Deviations from the brief
1. **A, B and C are one commit** (`7e8e516`). They share `settlement.py`, `chain.py`, `config.py` and the picker, so per-phase commits would not each be green; D and E are separate commits.
2. `results_path()` still returns a name (or `None`); the ordered list with capabilities is the new `result_paths()`.
3. ESPN dates are one-day requests, not ranges (range = 400).
4. Extra rule: `STAT_UNSETTLEABLE` beside `PERIOD_UNSETTLEABLE`.
5. Phase F skipped (see above).
6. The dry run had SGO and the Odds API keys blanked.
7. Tests I changed because the behaviour they pinned is what this brief removes: the "tennis is not fetched" test and the `atp/wta`-less default count (now 19), and the four resolver-order tests (rewritten around the new path order). No test was deleted to get green.

## Observations (not changed)
- **SharpAPI's page cap is what limits football volume.** In this run `fetch_counts` (now visible) showed 3 pages × 200 rows = 600 rows buying only **3 matches for the Premier League (1 in the window)**, 3 for La Liga, Serie A and Bundesliga, and 3 for MLB — about 200 rows per match, because each match arrives with corners/cards/half lines even though `SHARPAPI_MARKETS` asks for moneyline/spread/total. 14 of the 19 feeds reported `capped: true` (SharpAPI had more rows available than the pages fetched). Raising `SHARPAPI_MAX_PAGES` (each page is one request at 5.5 s) or narrowing the markets would give more matches; I did not change either.
- Stale local `.env` overrides and the untracked `_top_fb_evidence.py` are as before.
