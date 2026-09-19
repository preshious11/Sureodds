# Volume and ranking — report

Follows `RESULTS_COVERAGE_REPORT.md`. Suite: **764 passed** (was 704). Commits `ecd41c5` (Phase 2), `8528f12` (Phase 3), `c50e271` (Phase 4); Phases 0 and 1 are measurement only. Nothing pushed. The Top ticket's code and every gate/cap are untouched.

## Phase 0 — "Before", measured from the cache (zero calls)

Source: the previous dry run's `provider_cache` (SharpAPI, 43 in-window events). Limits of this evidence: the cache holds *parsed, in-window* events; a line's bookmaker rows are `len(book_prices)`, so "rows" here are the parsed rows of in-window matches, not the raw feed. The raw feed for that run was **6,645 rows for 43 in-window matches** (37 requests) — the other ~80% of the rows belonged to matches after the window, or were player/other rows the parser drops (`PLAYER_PROP_UNSETTLEABLE` 164). Main vs alternate is inferred (line whose two sides are closest to 50%), because the cached line no longer carries `is_main_line`.

| League (in-window) | matches | rows | rows/match | useful rows* | useful % | first → last kickoff (UTC) |
|---|---|---|---|---|---|---|
| england_-_premier_league | 1 | 62 | 62 | 10 | 16% | 09-19 16:30 |
| spain_-_la_liga | 2 | 152 | 76 | 31 | 20% | 16:30 → 19:00 |
| italy_-_serie_a | 2 | 147 | 74 | 20 | 14% | 16:00 → 18:45 |
| germany_-_bundesliga | 1 | 95 | 95 | 15 | 16% | 16:30 |
| france_-_ligue_1 | 4 | 175 | 44 | 24 | 14% | 18:45 |
| netherlands_-_eredivisie | 3 | 105 | 35 | 20 | 19% | 16:45 → 19:00 |
| portugal_-_primeira_liga | 2 | 70 | 35 | 12 | 17% | 17:00 → 19:30 |
| turkey_-_super_lig | 2 | 76 | 38 | 13 | 17% | 17:00 |
| mlb | 2 | 372 | 186 | 18 | 5% | 18:10 → 20:05 |
| wta / atp | 8 / 16 | 142 / 31 | 18 / 2 | 24 / 31 | 17% / 100% | 15:35 → 09-20 01:20 |
| **all** | **43** | **1,427** | 33 | **218** | **15%** | |

\* useful = full period, stat `points`, main line, entity `all` (what the model prices for 1X2 / totals / handicaps).
Where the rows went (football, parsed rows): alternate goal totals 284, alternate handicaps 148, corners (all kinds) ≈ 270, cards 18, first/second-half goal totals 16; main result 75, main totals 40, main handicaps 30. A recorded page (P1 below) shows the same at the feed level: 200 rows for **one** match, 125 of them alternate lines, 34 `team_total_shots`, 24 `team_total_fouls`, 16 `exact_total_goals`, 10 `total_corners`…

## Phase 1 — what SharpAPI can filter (8 requests, all listed)

**Documentation read** (docs.sharpapi.io/en/api-reference/odds and /overview, sharpapi.io/pricing): `GET /odds` accepts `sportsbook`, `sport`, `league`, `market` (category aliases `main|spread|total|props` **or exact market names**), `market_segment`, `event_id`, `selection`, `is_live`, **`is_main_line`**, **`is_alternate_line`**, `group_by`, `limit` (max 200), `offset` (≤ 500, else `400 offset_too_large`), `cursor`. `GET /events` (sport, league, status, date, q, limit ≤ 200) lists fixtures with `book_count` / `market_count` and no odds.

**Probes** (all `GET`, `league=england_-_premier_league`, `is_live=false`; paced ≥ 5.5 s; free-tier key):

| # | Request (extra params) | Result |
|---|---|---|
| P1 | `limit=200 market=moneyline,spread,total` (the old request) | 200 rows, **1 match** (200 rows/match); main 75 / alternate 125; types dominated by `team_total_shots` 34, `point_spread` 24, `team_total_fouls` 24, `total_goals` 21… ; more data available |
| P2 | P1 + `market_segment=full_game` + `is_alternate_line=false` | **0 rows** — full-game rows have `market_segment: null`, so `full_game` matches nothing |
| P3 | P1 + `is_main_line=true` | 200 rows, **3 matches** (66.7 rows/match), all main; still shots/fouls/exact-total/corners types |
| P4 | `limit=500` + P2's filters | 0 rows (P2's filter) but the response echoes `pagination.limit: 200` — **200 is the maximum, silently clamped**, no error |
| P5 | `market=moneyline,point_spread,total_goals,double_chance,draw_no_bet,both_teams_to_score,team_total_goals` + `is_main_line=true` | 200 rows, **7 matches (28.6 rows/match)**, spanning 2026-09-19 → 10-10; every requested type accepted; `market_segment` null on all |
| P6 | `market=moneyline,point_spread,total_goals` (alternates included) | 200 rows, 4 matches (50 rows/match); main 58 / **alternate 142** |
| P7 | `GET /events?league=…&status=upcoming&limit=50` | 50 fixtures (`sport, league, start_time, book_count, market_count, markets, books`); includes novelty "1st Booking" fixtures with one market |
| P8 | `league=mlb market=moneyline,spread,total is_main_line=true` | 200 rows, 6 matches (33.3/match); 90 of them `player_total_bases` (dropped by the parser); exact names seen: `moneyline`, `run_line`, `total_runs`, `team_total` |

Recorded rows for P5 and P8 are in `tests/fixtures/sharp_pl_wide_main.json` and `sharp_mlb_main.json`.

**Quota.** Response headers on every probe: `X-Ratelimit-Limit: 12`, `X-Ratelimit-Remaining`, `X-Ratelimit-Reset` (unix seconds), `X-Data-Delay: 60`. No `x-quota-*` / usage header exists. The docs' plan table: Free 12 requests/min (Hobby 120, Pro 300, Sharp 1,000); the pricing page: "12 requests/min, approximately 17,280 requests per day **with no monthly cap**". So **SharpAPI publishes no monthly (or daily) limit** — only the per-minute limit, which the 5.5 s spacing already respects. I therefore did **not** assume 1,000/month; `SHARPAPI_MONTHLY_REQUEST_LIMIT=3000` is *our own* budget, not the provider's, and says so in the code and `.env.example`.

## Phase 2 — volume fix (`ecd41c5`)

Changes: every `/odds` request now carries `is_main_line=true` (`SHARPAPI_MAIN_LINES_ONLY`, default on) and, for football and baseball, **exact market names** (`SHARPAPI_MARKETS_FOOTBALL`, `SHARPAPI_MARKETS_BASEBALL`); other sports keep the `SHARPAPI_MARKETS` aliases plus `is_main_line`. Request budgeting: `SHARPAPI_MONTHLY_REQUEST_LIMIT` → `Budget.monthly_daily_allowance` (10% held back) capped by `SHARPAPI_MAX_REQUESTS_PER_GENERATION=60`; feeds are walked in `SHARPAPI_LEAGUE_PRIORITY` order (PL, La Liga, Serie A, Bundesliga, Ligue 1, Champions League, then the rest); each feed still to come is owed one page; a feed with no budget makes **no** request (`stop_reason: budget`, checked before the request). `SHARPAPI_MAX_PAGES=3` stays as the per-feed ceiling. Stop conditions: `no_more_data`, `window_end`, `events_per_target`, `max_pages`, `budget`, `error`.

Bug fixed on the way: the old "past the window" check used the page's **earliest** kickoff, so a page holding one in-window match and later ones still triggered another request; it now uses the **latest** (the feed is kick-off ordered; P5 confirms ascending order across the page). This is the "check after the request it guards" case — the request budget check now runs before the request too.

Instrumentation: each feed reports `requests, rows, matches_seen, matches_in_window, rows_per_match, capped, stop_reason`; the generation report carries `requests: {spent, budget}`; `/lastrun` prints them ("SharpAPI feeds").

**Before / after** (same league set, 19 feeds, Saturday 2026-09-19; the "before" run was at 16:55 UTC, the "after" run at 17:43 UTC):

| | Before | After |
|---|---|---|
| Rows per football match (feed level) | ≈ 200 | **15–31** (PL 28.6, La Liga 25.4, Serie A 31.3, Ligue 1 25.0, UCL 15.4) |
| Matches seen per big-league feed | 3 (600 rows, capped) | **6–8** in one 200-row page (UCL 13) |
| Requests per generation | 37 | **19** (one per feed) of a budget of 60 |
| Raw rows fetched | 6,645 | **2,194** |
| Feeds reporting `capped: true` | 14 of 19 | **0 of 19** (every feed ended on `window_end` or `no_more_data`) |
| Football / baseball sport status | `partial` / `partial` | `success` / `success` |
| Baseball (MLB) in-window matches | 3 | **15** (all of today's slate; 179 rows) |

In-window matches for the top five leagues in the "after" run: PL 0, La Liga 1, Serie A 1, Bundesliga 0, Ligue 1 5. **The ≥ 8 target was not met, and volume is not why**: the run was at 17:43 UTC, so the window (start = now + 30 min, end = 02:00 UTC) had lost every early kickoff, and the next round is on Sunday, past the window end. Each feed fetched the whole remaining fixture list it could see (6–8 matches) in one request; nothing was truncated. A generation at the real POST_TIME (09:00 WAT = 08:00 UTC) would see the whole Saturday slate; I could not test that today and did not raise the budget to chase the number.

## Phase 3 — wider football markets (applied)

Condition: a football match costs ≤ 80 rows with the wider list — **28.6 rows/match** (P5) and the request budget is 19 of 60 → applied. Default `SHARPAPI_MARKETS_FOOTBALL=moneyline,point_spread,total_goals,double_chance,draw_no_bet,both_teams_to_score,team_total_goals` (exact names, **deviation**: the brief's aliases `spread`/`total`/`team_total` also pull shots/fouls/corners rows — 66.7 rows/match measured in P3 — so exact names are used). One parser gap found and fixed: `team_total_goals` rows (`selection_type` `home_over`…) were silently dropped (the generic `total` branch wanted `over`/`under`); they now become team totals (`market total`, `entity home|away`). Tests: one per market type from recorded P5 rows, the four team-total selections, and a guard that shots/fouls/exact team totals are still not read as goals.

Consequence to know about (not a bug): with `is_main_line=true` **alternate lines no longer enter the pool** — no more Over 1.5 / +1.5 lines except where the main line *is* that line. The Daily therefore leans on main lines, double chance, DNB, BTTS and team totals. `SHARPAPI_MAIN_LINES_ONLY=0` brings the old lines back at ~3× the rows.

## Phase 4 — family-fair Daily ranking (`c50e271`)

`DAILY_RANKING=family_z` (default) / `raw` (rollback, tested to reproduce the `_daily_score` order exactly). New `selection/families.py`: line-aware classifier (`win`, `total_main|alt`, `handicap_main|alt`, `safety`, `btts`, `team_total`, `period`, `exotic`, `prop`, `other`; main line = the line whose de-vigged sides are closest to 0.5, a lone line is main, handicaps keyed by the home line) and a z-score within family (families with < 3 candidates use the pool's mean/std; std floor 0.02). Candidates are sorted by (z, score); everything after that — gates, `preferred p ≥ 0.5`, one pick per match, all caps, the relaxed pass, conflicts, combined-odds sizing — is unchanged. Diagnostics: `daily_family_pool`, `daily_family_picks`, `daily_ranking`, per-pick `notes["family"]` / `notes["z"]`; `/lastrun` "Daily by family".

**Synthetic pool** (12 safe alternate handicaps, 4 wins, 4 main totals, caps loosened in the test only to isolate ordering): `raw` Daily = 9 `handicap_alt`, 0 `win`, 0 `total_main`; `family_z` Daily = 5 `handicap_alt`, 1 `win`, 1 `total_main` (Top identical in both).

**Real cached events** (the dry run's SharpAPI payload, re-scored offline under each mode; small differences from the live card are from the scoring clock):

| Daily family mix | `raw` | `family_z` |
|---|---|---|
| win | 3 | 2 |
| safety (dc/dnb) | 3 | 2 |
| handicap_main | 2 | 1 |
| total_main | 0 | **1** |
| team_total | 0 | **1** |
| Daily picks | 8 | 7 |

The live dry run's Daily (family_z): safety 1, handicap_main 2, win 2, total_main 1, btts 1. Honest reading: on this evening's pool the effect is modest, because Phase 2's main-line filter already removed the alternate lines that used to crowd out wins and overs (the previous dry run's Daily was Over 1.5 / +1.5 lines); `family_z` adds a main-line Over 2.5 and a team total in place of a handicap and a double chance. The Top ticket is identical under both modes (Le Mans or Lorient 1.32 + Rangers +1.5 1.52 = 2.01).

## Final dry run (`cli.py picks --dry-run`, SharpAPI only; SGO and Odds API keys blanked)

`fetch_counts` per feed (`requests / rows / matches seen / in window / rows per match / capped / stop`):

```
england_-_premier_league        1 200  7  0 28.6 no window_end
spain_-_la_liga                 1 178  7  1 25.4 no no_more_data
italy_-_serie_a                 1 188  6  1 31.3 no no_more_data
germany_-_bundesliga            1 114  6  0 19.0 no no_more_data
france_-_ligue_1                1 200  8  5 25.0 no window_end
uefa_-_champions_league         1 200 13  0 15.4 no window_end
england_-_championship          1   0  0  0  0.0 no no_more_data
netherlands_-_eredivisie        1  38  2  2 19.0 no no_more_data
portugal_-_primeira_liga        1 108  6  2 18.0 no no_more_data
turkey_-_super_lig              1 108  6  2 18.0 no no_more_data
uefa_-_europa_league            1   0  0  0  0.0 no no_more_data
uefa_-_europa_conference_league 1  36  2  0 18.0 no no_more_data
nigeria_-_premier_league        1   0  0  0  0.0 no no_more_data
atp                             1   9  5  5  1.8 no no_more_data
wta                             1 200 27  5  7.4 no window_end
nba                             1 200 25  0  8.0 no window_end
nfl                             1 200  8  0 25.0 no window_end
mlb                             1 179 15 15 11.9 no no_more_data
euroleague                      1  36  6  0  6.0 no no_more_data
```

Requests used: **19 of a budget of 60** (37 before). Picker: `pool=275 eligible=262 top_pool=37 daily_pool=118 -> top=2 daily=7 ticket=17.41`; no `NO_RESULTS_PATH`; `selection_rejections {'INVALID_PRICE': 13}` (STAT/PERIOD rejections disappear because those rows are no longer requested). Sport status: football 13 events, tennis 10, baseball 15, basketball and NFL empty (out of window). Daily family pool `{'btts': 9, 'win': 21, 'handicap_alt': 3, 'team_total': 16, 'total_main': 24, 'handicap_main': 23, 'safety': 21, 'other': 1}`, picked `{'safety': 1, 'handicap_main': 2, 'win': 2, 'total_main': 1, 'btts': 1}`.

Top (unchanged logic, 2.01): Le Mans v Lorient — Le Mans or Lorient @ 1.32; Texas Rangers v Blue Jays — Rangers +1.5 @ 1.52.
Daily (8.68 on its own): Toulouse (DNB) 1.28; ESTAC Troyes +1.5 1.20; Dodgers to win 1.24; Over 2.5 goals Venezia–Lazio 1.69; Tigers +1.5 1.51; BTTS Yes Lyon–Rennes 1.44; Rafael Jodar to win 1.24. Every `Odds:` line is the plain price.

## Deviations and notes
1. Exact market names per sport instead of the aliases (measured 2.3× cheaper); non-football/baseball sports keep the aliases.
2. `is_main_line=true` removes alternate lines from the pool (see Phase 3); that is a behaviour change the volume fix needs, and it is one env var to undo.
3. `SHARPAPI_MONTHLY_REQUEST_LIMIT` is our own cap, not SharpAPI's; the provider documents none.
4. Two of the eight probes (P2, P4) returned zero rows because `market_segment=full_game` matches nothing; P4 still answered the `limit=500` question via the echoed `limit: 200`.
5. The ≥ 8-matches-per-league target was not met in the final run for a reason unrelated to volume (time of day); not tested at POST_TIME.
6. Nothing was called on the Odds API or SGO; ESPN / football-data / EuroLeague were not used.
