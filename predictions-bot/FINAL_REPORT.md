# Final report — merge fix, doubles guard, performance pass

Follows `CROSS_TIER_REPORT.md`. Suite: **848 passed** (was 764 before the cross-tier work, 817 at its end). Nothing pushed. Commits: `ed7f70e` (A1), `ef7e869` + `d7596e1` (A2), `98c6b40` + `d9d6350` (B0), `b4af092` (H1), `b2d0b17` (H5/H7), `7425747` (H12/H15), `d03f917` (H14), `e7df20e` (H16), `9df68cd` + `9d9b118` (H19).

---

## Part A — what gets picked and priced

### A1. Doubles
- **What:** neither the SharpAPI `/odds` rows nor the Odds API carry a doubles flag or a participant count (checked in the recorded rows and parsers), so the indicator is the pair separator `" / "` in either side (`util/names.py::is_doubles`). SharpAPI and Odds API tennis events are dropped at parse time with `parse_counts["DOUBLES_UNSUPPORTED"]`; `/lastrun` now prints a "Parse rejections" block. `result_paths()` returns no path for a tennis doubles event, so anything that slipped past is rejected `NO_RESULTS_PATH` by the gate. I did not extend the ESPN parser to doubles (no recorded fixture with two athletes per side).
- **Tests** (`tests/test_doubles.py`): the live card's `Dabrowski / Stefani vs Y. Dang / X. You` is rejected, a singles WTA event is kept, and a synthetic doubles event reaching the gate gets `NO_RESULTS_PATH`.
- **Replay day:** `DOUBLES_UNSUPPORTED = 3` on the SharpAPI report.

### A2. Providers now merge
`merge_events` uses `fixture_orientation` (one matcher), the competition veto (both resolved and different → never merge; otherwise the league id / normalised league name must agree case-insensitively), and re-expresses a swapped provider's markets on the kept event's sides.

- **Handicap convention (found while writing the test, and it fixed a bug of mine):** `MarketLine.line` is applied to the **selected side** (base.py docstring; `settlement._handicap(sel, opp, line)`). Relabelling home↔away therefore keeps the line's sign: "their home −1.5" is "our away −1.5" (the same team keeps the same line). `MarketLine.mirrored()` implements it; `tests/test_merge_providers.py::test_a_swapped_providers_handicap_grades_exactly_like_the_same_orientation_provider` grades the swapped provider's market, the merged market and the same-orientation market against one `MatchResult` (Rangers 5–4, 7–4, 5–5) and gets the same outcome each time. **`cross_tier.mirror` from the previous task negated the line — wrong; corrected** (it only ever affected a swapped handicap's DUPLICATE-vs-NESTED label, not whether the leg was refused).
- **Pooling** unions `book_prices`, recomputes `price` per `PRICE_MODE` (median), records both providers in `sources`, keeps each event id in `alt_ids`; `n_books` is the union.
- **Two problems the recorded day exposed, beyond the brief:**
  1. SportsGameOdds tags a moneyline / handicap with the selected team as its `entity` (`home`), SharpAPI with `all`. One bet, two keys → nothing was pooled after the merge and the Angels +1.5 leg still existed twice. The pooling key now ignores `entity` for result/handicap/DNB/DC markets (team totals stay separate). Test included.
  2. SportsGameOdds lists a football market under both its `game` and `reg` period, both mapped to `full`: `BTTS Yes` appeared twice on one MLS event (4 books @1.5128 and 1 book @1.5) and made the same tip a Top leg **and** a Daily candidate — the one remaining `CROSS_TIER_DUPLICATE` after the merge. `consolidate_lines()` now collapses identical-key lines (most-quoted line's price kept, books unioned) whenever provider events are restored or collected. Tests included.
- **Diagnostics:** `merged_events`, `merged_pairs_by_league`, `merged_pairs` in the generation diagnostics, a "Merged across providers" block in `/lastrun`, and `merged N events across providers` in the log.
- **Merged pairs on the recorded day** (15 of 15 MLB games; SharpAPI → SportsGameOdds; none swapped): Chicago White Sox v Detroit Tigers · Baltimore Orioles v Milwaukee Brewers · New York Mets v Philadelphia Phillies · Tampa Bay Rays v **BOS** Red Sox → Boston Red Sox · Cleveland Guardians v **Oakland** Athletics → Athletics · Cincinnati Reds v Chicago Cubs · Pittsburgh Pirates v Kansas City Royals · Texas Rangers v **TOR** Blue Jays → Toronto Blue Jays · Houston Astros v Atlanta Braves · St. Louis Cardinals v Washington Nationals · **ARI** Diamondbacks → Arizona Diamondbacks v New York Yankees · Colorado Rockies v Seattle Mariners · San Diego Padres v Miami Marlins · Los Angeles Dodgers v San Francisco Giants · Los Angeles Angels v Minnesota Twins. (Merge across league-id case `mlb` / `MLB` included.)
- **Cross-tier counters on the replay day: 0** (`CROSS_TIER_DUPLICATE`, `_NESTED`, `_REMOVED` all absent). The rule and its three layers are still in place for a pair that fails to merge. The replay slate has one match in both tiers with different outcomes: CF Montréal v Columbus Crew (Top: double chance 12; Daily: Over 2.5 goals).

---

## Part B — performance

**The honest shape of the problem.** Offline, one whole generation is under a second (0.86 s wall / 0.86 s CPU before). The wall-clock a subscriber's 09:00 job actually spends is the network: in the recorded live run SharpAPI took **100 s** (19 requests at the 5.5 s spacing the free tier's 12 req/min requires) and SportsGameOdds **64 s** (three 429 back-offs = 36 s of that), run one after the other. The big win is therefore overlapping the two providers, not CPU. CPU work was reduced where it was measurable, and every other hypothesis was measured and rejected.

**Method.** `tools/replay.py` replays one recorded generation (`tests/fixtures/replay/replay.db`: the two providers' `provider_cache` rows from a single live `--dry-run`, 1.3 MB, clock pinned to 17:39:34 UTC, network blocked) through collect → merge → score → select → store → render, /top, /daily, pagination callbacks and `deliver_picks` with 300 fake subscribers, and writes the slate to JSON. `tests/fixtures/replay/golden.json` was recorded after Part A (once re-recorded, in `d9d6350`, when I fixed the harness to take the cache-miss path instead of forcing a provider retry). **`tests/test_replay_golden.py` passes at every optimisation commit and at HEAD** — picks, order, prices, labels, cards, views, deliveries and diagnostics are byte-identical (timings stripped).

| Metric | Before | After | Change |
|---|---|---|---|
| Generation wall-clock, offline replay (median of 5) | 0.86 s | 0.57 s | −34 % |
| Generation wall-clock, live (SharpAPI 100 s + SGO 64 s) | 164 s serial | ≈100 s (**simulated** at 1:20 with the real durations: 8.2 s serial → 5.0 s concurrent; the one allowed live generation was spent recording the replay day) | −39 % (bounded by SharpAPI's own pacing) |
| Generation CPU: score / select | 0.69 s / 0.125 s | 0.47 s / 0.047 s | −32 % / −62 % (Daily build 0.096 → 0.020 s wall) |
| Requests per provider per generation | SharpAPI 19, SGO 37 objects | 19, 37 (no code path that requests changed) | equal |
| SQLite executes / commits per generation | 14 / 2 | 14 / 2 | already one transaction |
| SQLite executes / commits, delivery of 300 | 303 / 303 | 303 / 18 | −94 % commits |
| `/top` latency, warm p50 / p95 | 0.79 / 1.31 ms | 0.80 / 1.32 ms | none needed |
| `/top` latency, generation holding the lock, p50 / p95 | 0.72 / 1.21 ms | 0.70 / 1.15 ms | none needed |
| Delivery, 300 subscribers, 30 ms fake send | 9.6 s | 15.2 s (paced at ≤ 20 msg/s) | slower — see H15 |
| Delivery, 300 subscribers, 150 ms send (a real Telegram round trip) | 45.7 s | 15.3 s | −67 % |
| Settle run wall-clock (live, free sources) | ≥ 11.75 s (sum of its own 11 request times; pacing excluded) | 8.68 s | −26 % vs the lower bound |
| Cold start to first `getUpdates` | 0.50 s | 0.50 s | none needed |
| Peak RSS, replay process | 53.1 MB | 53.9 MB | equal |
| Peak memory while parsing SGO (synthetic: 36 events × 1,200 odds × 8 books) | 156.6 MB | 70.9 MB | −55 % |

### Hypotheses

| # | Verdict | Evidence |
|---|---|---|
| H1 providers sequential | **ACCEPTED** | `collect_persisted` now fetches every stale provider in its own thread and merges in `PROVIDER_ORDER`; 164 s → ≈100 s (simulated with the measured durations); golden identical; tests: overlap, provider-order merge, one failing provider does not delay the other, a fresh cache row is not refetched. |
| H2 catalog calls each run | **REJECTED / N/A** | SharpAPI's `/sports` catalog is already cached in SQLite (`capabilities.load_cached`) and the generation makes no catalog call (19 requests = 19 feeds). SGO `/leagues` and the Odds API `/sports` are memoised per process: 1 call, 2.9 s, once per process start. The Odds API per-key `/events` calls could not be measured (zero Odds API calls allowed, key at 0 credits) and caching fixture lists for a day would change which events appear intraday — not done. |
| H3 SharpAPI pages after the target | **N/A** | Live run: every feed made exactly 1 request and used its rows (`stop_reason` `window_end` / `no_more_data`); fixed in the volume brief. |
| H4 retry sleeps on the critical path | **ACCEPTED (with H1)** | SGO's 36 s of 429 back-off now overlaps SharpAPI's 100 s; test with a failing provider. |
| H5 Poisson grid | **ACCEPTED (partly)** | Lambdas are already computed once per event (36 calls / 43 events), not per market. `ScoreGrid.__init__` now looks the Poisson terms up per row/column and applies τ only to the four low-score cells — the same floating-point operations in the same order, so results are bit-identical: score wall 0.69 → 0.47 s. Rounded-lambda caching not attempted (would risk the golden for little). |
| H6 Top exhaustive search | **REJECTED** | 17 ms of a 118 ms select, pool of 161 legs; not the bottleneck. (Separately, the 300-leg Top test had regressed past its 2 s limit because of the cross-tier linking; a `similarity` floor shortcut that proves a pair is below the threshold without running the matcher — identical answers, tested — brought it to 1.1 s.) |
| H7 Daily build | **ACCEPTED** | The cost was not `fits()` but re-deriving each event's fixture identity for every candidate × Top leg; it is now computed once per event per selection. Daily build 0.096 → 0.020 s wall (with the similarity shortcut). |
| H8 memoise label/period formatting | **REJECTED** | Not in the profile's top 30. |
| H9 f-string logging in hot loops | **N/A** | No `log.debug(f"…")` in the code base. |
| H10 provider payload JSON | **REJECTED** | SGO day: `loads` 11.7 ms + encode 19 ms + `dumps` 11.5 ms ≈ 42 ms per generation; compact separators save 8 % of bytes and ~1 ms. Not worth changing the stored bytes. |
| H11 per-statement commits | **N/A** | Generation is already one transaction (14 executes / 2 commits). |
| H12 N+1 queries | **ACCEPTED (delivery)** | `deliveries_done()` one query instead of one per subscriber; the per-pick checks in `store_picks` are ≤ 10 rows. Indexes: `deliveries` is keyed by (kind, date, chat) and `picks` has the date/status indexes; none missing. |
| H13 snapshot deserialisation | **REJECTED** | A whole `/top` (read + render + fake send) is 0.8 ms; nothing to gain. |
| H14 command loop | **ACCEPTED** | `/top` during generation was already < 2 ms (the read path never waits for the generation lock) and a burst of updates was already processed in full before the next long-poll. The real hole: `/run`, `/settle`, `/recap` ran synchronously on the poll thread, freezing every command for the length of the job. They now run on their own thread (same reply text, sent when done; an in-flight action is joined when the loop stops). Test. |
| H15 delivery | **ACCEPTED, with a caveat** | Card rendered once per zone (4 renders, not 300), one query for done-deliveries, sends through 8 workers paced at ≤ 20 messages/s, deliveries recorded in batches of 20 in one transaction each; "joined before target", once-per-day and retry-on-failure preserved (tests). With a real 150 ms round trip: 45.7 s → 15.3 s. With the brief's 30 ms fake the 20 msg/s cap makes it slower (9.6 → 15.2 s): the cap, not the pool, is the limit. Below 8 recipients sends stay sequential. |
| H16 settlement | **ACCEPTED** | One thread per first-choice source (football-data.org's 6.5 s pacing overlaps ESPN's and EuroLeague's), each source asked by one thread at a time, every answer — including "nothing found" — cached so the grading pass never asks twice. The per-run caches are keyed by (competition, day): the test shows 4 football picks and 3 baseball picks costing 1 request each. Live run: 11 requests (9 ESPN, 1 football-data, 1 EuroLeague), **8.68 s wall vs 11.75 s of summed request time**; 3 picks settled, 28 stay pending (no result path or not on the scoreboard). Outcomes identical to a run with the prefetch switched off (test). |
| H17 scheduler tick | **REJECTED** | The "no picks yet" check is one indexed query, 0.01 ms every 30 s. (Jobs already run on their own threads.) |
| H18 cold start | **REJECTED** | 0.50 s from process start to the first `getUpdates` (0.45 s of it imports). |
| H19 memory | **ACCEPTED** | Every raw SGO page was held until all leagues were fetched; pages are now parsed as they arrive and released. Synthetic worst case (36 events × 1,200 odds × 8 books): 156.6 → 70.9 MB. `provider_cache` reads already load only today's row per provider. The live process's RSS was not recorded during the one live generation. |

### Not measured / limits
- Live generation wall-clock is a simulation with the real per-provider durations; the single allowed live generation was used to record the replay day, and running it again would have cost SharpAPI/SGO quota the brief forbids.
- The settle "before" is a lower bound (sum of the run's own request durations), not a second live run.
- Peak RSS for a live generation was not captured; the −55 % is on a synthetic payload sized from the live run's rejection counts.

---

## Live calls, production data, files

- **Provider calls across the brief:** one live generation (SharpAPI 19 requests, SportsGameOdds 37 objects; three SGO 429s retried by the client), one live settlement run (ESPN 9, football-data.org 1, EuroLeague 1). Odds API: **0**. A first settlement attempt exited on the `SHARPAPI_KEY is required` check before any request (0 calls) and was re-run with a placeholder key that no results path uses.
- **Production data:** `DB_PATH` was never pointed at `data/predbot.db`; every run used a temp copy with Telegram blanked (`--dry-run`, or the replay's fake client). Before → after: `data/predbot.db` prediction_cache 0 / picks 0 / posts 0 / deliveries 0 / provider_cache 0 → identical, file time 12:39 unchanged; `data/predbot.backup-before-clear.db` 0 / 25 / 0 / 2 / 0 → identical, 12:39 unchanged. All temp databases were deleted.
- **Files touched:** `predbot/bot/{broadcast,commands,lastrun}.py`, `predbot/cache/providers.py`, `predbot/jobs/{daily_picks,settle}.py`, `predbot/markets/settlement.py`, `predbot/model/poisson.py`, `predbot/providers/{base,chain,oddsapi,sharpapi,sportsgameodds}.py`, `predbot/selection/{cross_tier,picker}.py`, `predbot/storage/db.py`, `predbot/util/{names,timing}.py`, `tools/replay.py`, `tests/fixtures/replay/{replay.db,golden.json}`, `tests/test_{collect_concurrent,delivery_batch,doubles,merge_providers,replay_golden,settle_parallel,sgo_streaming,cross_tier}.py`.
