# Sport coverage audit — why only football and baseball?

Read-only audit, written 2026-09-19. Nothing but this file was created; no commits, no live provider calls. Secrets are not shown.

## Evidence sources and their limits (read first)

| Source | State | Consequence |
|---|---|---|
| Local DB `data/predbot.db` | **Empty.** I cleared every table at the user's request earlier today, after copying it to `data/predbot.backup-before-clear.db`. | Nothing recent to read from it. |
| Backup DB (opened `mode=ro`) | `prediction_cache` = 0 rows, `provider_cache` = 0 rows (both were already cleared before the backup was taken; `provider_cache` had 21 rows that are gone). `picks` = 25 rows, all `2026-09-18` (Friday), all from the **pre-reconfiguration** code (`api_football` origin). `api_budget` = 24 rows, `kv` = 3 rows. | There is **no stored last-run diagnostic** in any DB. |
| Logs | `bot.log` / `bot.out.log` are from 2026-09-17, pre-reconfiguration; `prediction_diagnostic.json` (2026-09-19 10:53 UTC) is an empty run (`providers: {}`, all counts 0). | Not usable for item 8. |
| **"Last run" used below** | The live `python cli.py picks --dry-run` I ran on 2026-09-19 12:03–12:07 UTC (temp DB, nothing posted). Its raw log and selection diagnostics survive in this session's scratchpad (`dry_out.txt`, `dry_diag.txt`). `RECONFIGURATION_REPORT.md` quotes an excerpt. | Items 5, 7, 8, 9 rest on this one Saturday run, not on production. It ran **before** `atp,wta` were removed from `SHARPAPI_LEAGUES`. |
| Production (justrunmy.app container) | **Not available.** Its dashboard env vars override the defaults, and its DB/logs are not on this machine. | The audit describes what the code and the local config produce. Whether production matches (e.g. `FOOTBALL_DATA_KEY` set, stale `ALLOWED_SPORTS`, an old `SGO_LEAGUES`) is **unknown**. |
| Local `.env` | Sets none of the six variables in item 1 (checked by name; values not printed). Effective values = code defaults. | Item 1 is the local effective config only. |

---

## 1. Effective sport and league inventory — CONFIRMED

Resolved by `load_config()` (no network). `config.py:162` ALLOWED_SPORTS, `:143` SHARPAPI_LEAGUES, `:148` SHARPAPI_SPORTS, `:134-135` SGO_SPORTS / SGO_LEAGUES, `:126` ODDS_API_LEAGUE_ALLOWLIST.

- **ALLOWED_SPORTS**: football, basketball, american_football, baseball, tennis.
- **SHARPAPI_SPORTS**: empty (sport-level feeds are not used; only explicit leagues).
- **SGO_SPORTS**: empty (= no sport filter); **SGO_LEAGUES**: NBA, NFL, MLB, MLS, NCAAF, NCAAB, UEFA_CHAMPIONS_LEAGUE.
- **ODDS_API_LEAGUE_ALLOWLIST** (11, all `soccer_*`): epl, spain_la_liga, italy_serie_a, germany_bundesliga, france_ligue_one, uefa_champs_league, uefa_europa_league, efl_champ, netherlands_eredivisie, portugal_primeira_liga, turkey_super_league.
- **SHARPAPI_LEAGUES** and the sport `sport_for_league()` (`providers/sharpapi.py:116`, tables at `:45-53`) assigns:

| Slug | Sport | Slug | Sport |
|---|---|---|---|
| england_-_premier_league | football | uefa_-_europa_league | football |
| spain_-_la_liga | football | uefa_-_europa_conference_league | football |
| italy_-_serie_a | football | nigeria_-_premier_league | football |
| germany_-_bundesliga | football | nba | basketball |
| france_-_ligue_1 | football | euroleague | basketball |
| england_-_championship | football | nfl | american_football |
| netherlands_-_eredivisie | football | mlb | baseball |
| portugal_-_primeira_liga | football | | |
| turkey_-_super_lig | football | | |
| uefa_-_champions_league | football | | |

**What each sport can be produced by**

| Sport | Provider(s) that can supply it |
|---|---|
| football | Odds API (11 soccer keys), SharpAPI (13 slugs), SGO (MLS, UEFA_CHAMPIONS_LEAGUE) |
| basketball | SharpAPI (nba, euroleague), SGO (NBA, NCAAB). **Not** the Odds API. |
| american_football | SharpAPI (nfl), SGO (NFL, NCAAF). **Not** the Odds API. |
| baseball | SharpAPI (mlb), SGO (MLB). **Not** the Odds API. |
| tennis | **nothing** |

- **Tennis has no league in any provider — CONFIRMED.** SharpAPI: `atp`/`wta` were removed from the default (commit f8afc7f; `LEAGUE_SPORTS` still knows them). SGO: no tennis league in `SGO_LEAGUES`. Odds API: `OddsApiProvider.sports = ["football","tennis"]`, but the allowlist is applied to tennis keys too (`providers/oddsapi.py:231`), and the allowlist holds only `soccer_*`, so zero tennis keys survive (the dry run showed `tennis: leagues_total 0`).
- **MLS is reachable only via SportsGameOdds — CONFIRMED.** It is in `SGO_LEAGUES` only; there is no `mls` slug in `SHARPAPI_LEAGUES` and no `soccer_usa_mls` key in the Odds API allowlist. (The `SHARP_TO_SGO_LEAGUE` map at `markets/settlement.py:258` has an `mls` entry, but that maps a slug for settlement only; nothing fetches it from SharpAPI.)
- `ALLOWED_SPORTS` is not a limiter: it lists all five sports, and `chain.filter_allowed` (`providers/chain.py:118`) only drops sports outside the list.

## 2. Day window — CONFIRMED (weekday list: PARTIAL, from general schedule knowledge, not from code)

Code: `jobs/daily_picks.py:66-69`; the fetch side in `cache/providers.py:69` uses the same end.

```
start = now + MIN_LEAD_MINUTES(30)
end   = min( local_midnight_end_of_today + DAY_WINDOW_END_OFFSET_HOURS(3),  now + PICK_WINDOW_HOURS(24) )
```

At POST_TIME 09:00 `Africa/Lagos` (WAT = UTC+1; 08:00 UTC): `start` = 09:30 WAT (08:30 UTC); midnight+3h = 03:00 WAT the next morning (02:00 UTC), earlier than now+24h (09:00 WAT next day), so **`end` = 03:00 WAT (02:00 UTC), a 17.5-hour window**. (Local `.env` sets `PICK_WINDOW_HOURS=24`, `MIN_LEAD_MINUTES=30`, `POST_TIME=09:00`, `TIMEZONE=Africa/Lagos`; `DAY_WINDOW_END_OFFSET_HOURS` is not set locally → default 3.)

In US Eastern that is **04:30 ET → 22:00 ET** (EDT, UTC-4) or 03:30 → 21:00 ET (EST, from early November). Applying it to the usual US kickoffs (schedule from general knowledge, not the repo):

- NFL Sunday 13:00 ET and the 16:25/20:20 ET games: inside the Sunday window. Thursday and Monday 20:15 ET = 00:15 UTC next day: inside (before 02:00 UTC).
- NCAAF Saturday 12:00 ET onward: inside up to about 22:00 ET; late games kicking off after ~22:00 ET (02:00 UTC) fall outside.
- So american_football events can appear on **Thu, Fri (NCAAF), Sat, Sun, Mon**; **Tue/Wed only occasionally** (mid-season NCAAF weeknight games). On a Tuesday or Wednesday the sport is usually absent for calendar reasons.

## 3. Sport share cap — (a) CONFIRMED, (b) NOT CONFIRMED, (c) CONFIRMED

Code: `selection/picker.py:487` (`sports_available = {sports in daily_pool}`), `:499`, `:502-504`, `:557`, relaxed pass `:600-615`. Default `DAILY_MAX_SPORT_SHARE=0.60` (`config.py:228`; local `.env` 0.60).

- **(a) Applies only with ≥ 2 sports** — CONFIRMED: `sport_share = cfg.daily_max_sport_share if len(sports_available) > 1 else 1.0`. Note the count is of sports in the **daily pool** (after eligibility), not sports fetched.
- **(c) One sport → not applied** — CONFIRMED (`1.0`).
- **(b) "guarantees the smaller sport ≥ 40%" — NOT CONFIRMED.** The rule is a *maximum* for any one sport, applied as each pick is added: `count + 1 > max(1, ceil(share × (len(chosen)+1)))`. It is not a minimum, and it does not make a sport appear. With two sports the smaller one ends with at least `N − ceil(0.6·N)` picks *if it has candidates*:

  | Daily picks N | Larger sport ≤ | Smaller ≥ |
  |---|---|---|
  | 2 | 2 (100%) | 0 |
  | 3 | 2 | 1 (33%) |
  | 4 | 3 | 1 (25%) |
  | 5 | 3 | 2 (40%) |
  | 7 | 5 | 2 (29%) |
  | 10 | 6 | 4 (40%) |

  40% holds only when N is a multiple of 5. Also, if the strict pass ends below `DAILY_COMBINED_MIN_ODDS`, a **relaxed pass waives the sport share** (`try_add(..., relaxed=True)`; recorded in `diagnostics["daily_caps_relaxed"]`). The list is sized by combined odds, not by count. The cap does not apply to the Top ticket.

## 4. Results gate coverage — CONFIRMED (with two observations)

`markets/settlement.py:274` `results_path`: order = SGO if the event came from SGO → Odds API `/scores` if the event came from the Odds API with a `soccer_*` key → football-data if the league maps to a competition → SGO if it covers the league. Evaluated with `load_config()` and the real function, with and without a `FOOTBALL_DATA_KEY`:

| Football league | Provider | Path **without** FOOTBALL_DATA_KEY (local `.env` has none) | Path **with** it |
|---|---|---|---|
| england_-_premier_league / soccer_epl | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi scores | football_data `PL` |
| spain_-_la_liga / soccer_spain_la_liga | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `PD` |
| italy_-_serie_a / soccer_italy_serie_a | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `SA` |
| germany_-_bundesliga / soccer_germany_bundesliga | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `BL1` |
| france_-_ligue_1 / soccer_france_ligue_one | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `FL1` |
| england_-_championship / soccer_efl_champ | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `ELC` |
| netherlands_-_eredivisie / soccer_netherlands_eredivisie | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `DED` |
| portugal_-_primeira_liga / soccer_portugal_primeira_liga | Sharp / Odds | Sharp: **NONE**; Odds: oddsapi | `PPL` |
| uefa_-_champions_league / soccer_uefa_champs_league | Sharp / Odds | sportsgameodds (UEFA_CHAMPIONS_LEAGUE); Odds: oddsapi | football_data `CL` (for non-SGO-sourced) |
| turkey_-_super_lig | Sharp | **NONE** | **NONE** |
| soccer_turkey_super_league | Odds | oddsapi scores | oddsapi scores |
| uefa_-_europa_league | Sharp | **NONE** | **NONE** |
| soccer_uefa_europa_league | Odds | oddsapi scores | oddsapi scores |
| uefa_-_europa_conference_league | Sharp | **NONE** | **NONE** |
| nigeria_-_premier_league | Sharp | **NONE** | **NONE** |
| MLS (SGO) | SGO | sportsgameodds | sportsgameodds |
| UEFA_CHAMPIONS_LEAGUE (SGO) | SGO | sportsgameodds | sportsgameodds |

Non-football for completeness: `nba`, `nfl`, `mlb` (Sharp) and NBA/NFL/MLB/NCAAF/NCAAB (SGO) → sportsgameodds. **`euroleague` (Sharp): NONE** (SGO_LEAGUES has no Euroleague), so any Euroleague pick is always `NO_RESULTS_PATH`.

- Sharp EL / ECL / Turkey / Nigeria have **no path and are rejected `NO_RESULTS_PATH` — CONFIRMED** (both columns; `picker.py:311-319`).
- Odds-API-origin `soccer_uefa_europa_league` / `soccer_turkey_super_league` **have a path via `/scores` — CONFIRMED in code** (prefix rule `ODDS_API_SCORES_PREFIXES = ("soccer_",)`, `settlement.py:252`, `:289-291`). Live behaviour is **unverifiable**: the key is at 0 credits, so `/scores` has never returned data here.
- In the dry run, `no_results_path_by_league` counted: PL 2, La Liga 3, Serie A 3, Bundesliga 3, Ligue 1 5, Championship 1, Eredivisie 3, Primeira 3, Super Lig 3 (= 26 football events) plus atp 18, wta 8. **EL, ECL and Nigeria had no events in that run**, so their rejection is by code, not observed.
- Observation: the gate labels any Odds-API-origin `soccer_*` event `oddsapi` regardless of the credit balance, so it stays optimistic while the quota is 0. That affects settling, not which sports are picked.

## 5. Odds API quota — CONFIRMED (reset date NOT RECORDED)

- Recorded (backup DB `kv`): `oddsapi:requests_remaining = 0`, `oddsapi:requests_used = 500`. The dry run also read the same values live from the free `/sports` and `/events` headers on every call (`x-requests-remaining=0 x-requests-used=500 reserve=80`), so it is current, not stale.
- Gate log: `OddsAPI: 11 in-season keys, 9 with events in window, spending <=9 credits on 3 keys …` → `x-requests-remaining=0, reserve=80 - refusing to spend 3 credits` → `quota/reserve gate stopped the odds fetch at soccer_efl_champ` → `0 odds rows -> 0 events in window (0 credits spent…)`. So **zero Odds API events entered the pool** (`can_spend_credits`, `providers/oddsapi.py:179-190`; `ODDSAPI_RESERVE_CREDITS=80`). The free `/events` listing did show 44 events in the window across 9 keys.
- Local counter (`api_budget`): `oddsapi` 36 credits on 2026-09-18, 36 for `2026-09`. The header total (500) is far larger, so the key's quota was consumed outside this bot's counter.
- **Reset date: not recorded** anywhere (no reset field in the code, the DB or the responses I have). It cannot be stated here.

## 6. SportsGameOdds allowance — computed

Code: `providers/budget.py:51-59`, `providers/sportsgameodds.py:126-127`, `:184-193`.

```
allowance = int( (monthly_limit - used_month) * (1 - 0.25) / days_left )
limit     = min(SGO_EVENT_LIMIT, allowance)
first-pass page_limit for league i = max(1, remaining // (n_leagues - i)), request limit=min(page_limit,100)
```

From the backup DB budget rows: `sportsgameodds` used `2026-09` = 62 (and 18 on 2026-09-19); `.env` `SGO_MONTHLY_OBJECT_LIMIT=1000`; September has 30 days, today is the 19th → `days_left = 12`.

`allowance = int(938 × 0.75 / 12) = 58`; `SGO_EVENT_LIMIT = 50` → **limit = 50** (the event limit binds, not the allowance). (With the now-empty DB: `int(1000×0.75/12) = 62` → still 50.) Seven configured leagues, all available at the tier (log: `8 leagues available`, no drop warning):

- If every league returns a full page: 7, 7, 7, 7, 7, 7, 8 objects (= 50).
- If earlier leagues return nothing: 7, 8, 10, 12, 16, 25, 50 (the later leagues inherit the spare).
- Leagues that report `nextCursor` are re-queried only while `events + page_limit ≤ 50` (`:214`).

So roughly **7 events per league on the first pass** across NBA, NFL, MLB, MLS, NCAAF, NCAAB, UCL — with MLB, MLS and NCAAF competing for the same 50 objects on a Saturday. The dry run spent 37 objects.

## 7. SharpAPI page cap — PARTIAL

- `SHARPAPI_MAX_PAGES=3` (`config.py:155`), `limit=200` per request (`providers/sharpapi.py:225`) → **at most 600 rows per feed**. `SHARPAPI_EVENTS_PER_TARGET=40` (`config.py:157`) can stop a feed earlier once 40 priced matches are seen.
- Rows per match observed in the last run: **5,706 rows over 105 raw matches ≈ 54 rows/match on average** (football 37, tennis 35, basketball 27, american_football 2, baseball 4), from 34 requests over 19 feeds (avg 1.8 pages/feed). At that density a 600-row feed holds **about 11 matches**. `fetch_counts` per league was **not logged or stored**, so per-league density is not recoverable.
- **Did any league hit the cap? — CONFIRMED for at least two feeds, not attributable to a league by direct evidence.** The last run's SharpAPI status was `football: partial (13 feeds)` and `baseball: partial (1 feed = mlb)`. In `fetch_events`, "partial" arises only when a feed added both "failed" and "fetched": either the page budget ran out (`while … else`, `:224`, `:266-269`) or a `ProviderError` occurred mid-walk. The run's log has no SharpAPI failure warning, so the page cap is the explanation. For `mlb` that means only **4 games** fit into 3 pages (about 150 rows per game — inferred from 600/4). MLB has far more games in a normal Saturday window, so SharpAPI's baseball coverage is truncated; SGO's MLB feed supplies more.

## 8. Last-run evidence (dry run, Saturday 2026-09-19 12:03–12:07 UTC)

Provider `sport_status` (from the log):

- **oddsapi**: football `empty` (0 fetched of 11 keys, gate stopped), tennis `failed` (0 keys).
- **sharpapi**: football `partial` 30 events (13 feeds), tennis `success` 29 (2 feeds), basketball `empty` 0 (2 feeds), american_football `empty` 0 (1 feed), baseball `partial` 4 (1 feed). Raw vs in-window: football 37/30, tennis 35/29, **basketball 27/0 (all outside window)**, **american_football 2/0 (outside window)**, baseball 4/4.
- **sportsgameodds**: `BASKETBALL empty (2 leagues)`, `FOOTBALL partial (2 leagues)`, `BASEBALL empty (1)`, `SOCCER empty (2)`; 37 events parsed. **These statuses are unreliable** — see the bug below.

Other diagnostics:

- `sport_rejected` (ALLOWED_SPORTS gate): not reported in that run, i.e. 0.
- `base_by_sport`: baseball 688, football 1140, american_football 266.
- `gte50_by_sport`: baseball 300, football 558, american_football 109.
- `top_eligible_by_sport`: baseball 77, football 101 (american_football 0).
- `daily_eligible_by_sport`: baseball 216, football 514, american_football 51.
- `selection_rejections`: NO_RESULTS_PATH 1158, INVALID_PRICE 88, DISABLED_MARKET 774; `top_pool_rejections` BELOW_THRESHOLD 1916; `daily_pool_rejections` BELOW_THRESHOLD 573.
- `no_results_path_by_league`: see item 4.
- Final picks (Top + Daily, `daily_ticket_by_sport`): **baseball 4, football 4, american_football 1**. Top: baseball 1 + football 1. Daily leagues: MLS 3, MLB 3, NCAAF 1.

Per sport — (a) never fetched, (b) fetched with zero events, (c) fetched with events but rejected, (d) picked:

| Sport | Verdict | Detail |
|---|---|---|
| football | **(d) picked**, and also (c) for part of the pool | 4 picks (all MLS, from SGO). 26 European-league events rejected `NO_RESULTS_PATH` (no FOOTBALL_DATA_KEY). Odds API football: (b) 0 events (quota gate). |
| baseball | **(d) picked** | 4 picks (MLB). |
| american_football | **(d) picked** (1, NCAAF) | 266 candidates, 51 daily-eligible, 1 chosen. SharpAPI `nfl`: (b) 2 raw events, both outside the window. |
| basketball | **(b) fetched, zero in-window events** | SharpAPI 27 raw events, all outside the window; SGO NBA/NCAAB raw events 0 (`empty`). |
| tennis | **(c) rejected `NO_RESULTS_PATH`** in this run (26 events: atp 18, wta 8) | **Now (a) never fetched**: the defaults no longer contain `atp`/`wta`, and no other provider has a tennis league. |

So this Saturday's slate was **football + baseball + one american_football pick**, not "football and baseball only".

**Diagnostics bug found (not a sport-dropping path):** `providers/sportsgameodds.py:245-262` builds `sport_status` keyed by the SGO sport id (`SOCCER`, `FOOTBALL`, `BASKETBALL`, `BASEBALL`), while the comment at `:79` and the event counter at `:260` use the canonical name (`football`, `american_football`). `e.sport in self.sport_status` never matches, so `events` stays 0 and `_merge_sport_state` can only return `empty`/`partial`/`failed`, never `success`. Result: the log shows `SOCCER: empty, events 0` although MLS events were parsed. The cache aggregation (`cache/providers.py:130-160`) then holds separate non-canonical keys (`SOCCER`, `FOOTBALL`, …) next to SharpAPI's canonical ones. It affects reporting only; it does not remove events.

## 9. Basketball season check — PARTIAL

- **Every basketball feed returned zero events — NOT exactly.** SharpAPI (`nba`, `euroleague`) returned **27 raw events, all outside the window** (`basketball:outside_window 27`, `in_window 0`), so zero *in-window* events; status `empty`. SGO NBA and NCAAB returned raw events = 0 (`BASKETBALL: empty`, and the `empty` state comes from the raw batch size, so it is reliable despite the bug above). The Odds API has no basketball feed by design. Why the 27 events were outside the window (a preseason or season-opener date) is not something the log records.
- **Nothing in the code excludes basketball when events exist — CONFIRMED.** `ALLOWED_SPORTS` lists it (`config.py:162`); `filter_allowed` only drops unlisted sports; the picker has no per-sport exclusion (the only sport reference in selection is the share cap, `:487-557`); there is a margin model (`model/ensemble.py:31`, `model/margin.py`); the results gate covers NBA/NCAAB via SGO (`settlement.py:255,258`). One exception: **SharpAPI `euroleague` events can never be picked** (no results path, item 4), so basketball from that league is dropped by design.

## 10. Weekday vs weekend — NOT AVAILABLE from the DB

- The backup DB has picks for **one date only: 2026-09-18 (Friday)**, 25 rows from pre-reconfiguration code: football 23, tennis 2 (WTA and ATP Challenger), no baseball, no american_football, no basketball. There is **no Saturday or Sunday slate** and nothing generated since the reconfiguration.
- Extra data points: the stored recap text (for Thursday 2026-09-17) shows `Baseball 3/5`, so a weekday slate did contain baseball. The only Saturday slate that exists is the unsaved dry run in item 8 (baseball 4, football 4, american_football 1). Not enough for a weekday-vs-weekend comparison.

## 11. Bookmaker/consensus text after "Odds:" (informational only)

`predbot/bot/format.py`:

- `:50` — `PickLine.odds_note` field.
- `:159-170` — `odds_note()` builds ` (median of N books)`, ` (consensus of N books)`, ` (Bet365)`.
- `:206` — `odds_note=odds_note(getattr(pick, "bookmaker", ""), getattr(pick, "n_books", 1))` fills the field.
- **`:248` — `f"Odds: {p.price:.2f}{p.odds_note}"` is the single line that appends the text.** Emptying the note (or dropping `{p.odds_note}` here) removes it from the card.
- Data source (upstream, leave alone): `cache/fetcher.py:236` and `:259` set `bookmaker` / `n_books`.

Tests that assert on it, all in `tests/test_phase5_card.py`: `:122` (`"Odds: 2.10 (median of 6 books)"`), `:124` (`"Odds: 2.35 (Bet365)"`), `:126` (plain `"Odds: 2.10"`, which would still pass), and `test_odds_note_edge_cases` `:129-133` (imports `odds_note` at `:10`). Other suites match plain `Odds: 2.05` lines (`test_picks_format.py`, `test_prediction_layout.py`, `test_outcome_format.py:155,197`, `test_accumulator.py:140`) and would be unaffected. So the removal is isolated to `format.py:248` (plus the helper) and the three tests above.

---

## Conclusion

The football-and-baseball mix on a given day is explained by items 1-9 without a code path that wrongly drops a sport. Tennis has no feed by configuration, and its results gate fails for it (items 1, 4); basketball was in the offseason or outside the window in the last run (item 9); american_football is calendar-bound (item 2) and did appear on Saturday; the Odds API contributes nothing while its quota is 0 (item 5); European football is cut by the results gate until a `FOOTBALL_DATA_KEY` exists (item 4), leaving MLS, MLB and American football as the eligible pool; and SharpAPI's 3-page cap and SGO's ~50-object budget limit how many MLB/MLS/NCAAF games get in (items 6, 7). Two claims in the brief did not hold as stated: the sport share cap is a maximum, not a 40% guarantee (item 3b), and basketball returned 27 SharpAPI events that were outside the window, not zero (item 9). I found no path that wrongly drops a sport or league that has events, a results path and a feed. Things I found but did not change: (1) SGO's per-sport `sport_status` uses SGO sport ids, so its events count and `success` state never work (`providers/sportsgameodds.py:245-262`), which misreports coverage but does not drop events; (2) SharpAPI `euroleague` can never be picked because no provider can settle it. The main caveat is that the evidence is a single Saturday dry run plus the code, because the DB caches are empty and the production container's config and data are not visible from here.
