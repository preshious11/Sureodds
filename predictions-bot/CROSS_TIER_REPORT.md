# Cross-tier duplicates and the Top ticket — report

Follows `VOLUME_AND_RANKING_REPORT.md`. Suite: **817 passed** (was 764). Commits: `0c17ffd` (Phase 0), `d999e8a` (Phases 1+2 together — the picker edits interleave, so they are one commit), `02385b4` (Phase 3), `e036293` (Phase 4), plus this report and the Phase 5 driver. Nothing pushed. Volume settings, family ranking, every gate and cap are untouched.

Owner's rule: *the same match may appear in Top and Daily, but with different outcomes.* Second requirement: the Top ticket must not come out empty when eligible legs exist.

## Phase 0 — reproduction (read-only)

**No database on this machine holds a real Top/Daily duplicate.** `data/predbot.db` is empty (0 picks); the only DB with picks is `data/predbot.backup-before-clear.db` (25 picks, one date, API-Football/Odds API/SharpAPI era). Searching it generously (same date and sport, kickoff ≤ 3 h, any team-name pair ≥ 0.6 similar) found two pairs, both **false positives** (Brentford v Chelsea vs Wexford v Cobh Ramblers, "Brentford" ~ "Wexford" 0.62). No SGO events are cached anywhere either. So, as the brief allows, the cases were built from the recorded fixtures and the owner's examples (`analysis/cross_tier_repro.py`), and the live run in Phase 5 supplied real provider names.

Why today's `same_fixture()` (before this work) returned False:

| Case (two providers, one match) | Old result | Reason |
|---|---|---|
| SharpAPI `LA Dodgers v San Diego Padres` / SGO `Los Angeles Dodgers v San Diego Padres` | False | `LA Dodgers` vs `Los Angeles Dodgers` = 0.69 < 0.82 |
| `TEX Rangers v BOS Red Sox` / `Boston Red Sox v Texas Rangers` | False | **home/away swapped** (home~away 0.92, away~home 0.88) |
| `Athletics v Seattle Mariners` / `Oakland Athletics v Seattle Mariners` | False | `Athletics` vs `Oakland Athletics` = 0.69 (the subset rule needs ≥ 2 tokens each side) |
| `LA Lakers v GS Warriors` / `Los Angeles Lakers v Golden State Warriors` | False | 0.67 and 0.69 |
| Football `Le Mans v Lorient` / `Lorient v Le Mans` | False | **home/away swapped** (1.00 / 1.00) |
| Football `Espanyol v Elche` / `RCD Espanyol v Elche CF` | False | `Espanyol` vs `RCD Espanyol` = 0.80 (`rcd` was not a generic club affix) |

Near-misses that were never blocked: the old cross-tier check compared **labels only** (`_duplicates_a_top_pick`), so "Arsenal or draw", "Arsenal DNB" and "Over 2.5" on a Top "Arsenal to win" / "Over 1.5" fixture were only stopped indirectly by the one-pick-per-match cap, never as nested tips.

## What changed

**Phase 1 — fixture identity** (`util/names.py`, `util/fixtures.py`, `selection/picker.py`)
- `same_fixture` accepts a home/away **swap**; `fixture_orientation(a, b)` returns `same` / `swapped` / `None`, and the picker remembers the swap (`link_events`) so selections are compared on the Top leg's side.
- `same_team(..., sport=)`: strips a leading/trailing all-caps 2–3 letter city code (`BOS`, `LA`, `GS`) and, for baseball / basketball / American football only, treats a name whose words are the *end* of the other's as the same team (`Athletics` / `Oakland Athletics`, `Rangers` / `Texas Rangers`). `Chicago White Sox` ≠ `Boston Red Sox`, `LA Lakers` ≠ `LA Clippers`. The 0.82 threshold is unchanged; `rcd`, `ud`, `sd` were added to the generic club affixes (`Espanyol` = `RCD Espanyol`). `_ALIAS` from the brief does not exist in this codebase; nothing to keep.
- When both events resolve to a competition (`competition_for_event`) and it differs, they never link (PL ≠ Championship however alike the names).
- `fixture_key(event)` = sport | competition (when known) | sorted canonical team pair | kickoff rounded to the nearest hour. The Phase 0 pairs produce identical keys (tests). It is a best-effort identity: when one provider's competition cannot be resolved the keys differ, and `fixture_orientation` remains the authority for linking.

**Phase 2 — the rule** (`selection/cross_tier.py`, `CROSS_TIER_SAME_FIXTURE`)
- `different_outcome` (default): a Daily candidate on a Top fixture is refused when, against any Top leg on that match (candidate mirrored first if the sides are swapped), it is the same selection or equivalent (`CROSS_TIER_DUPLICATE`), or **nested** (`CROSS_TIER_NESTED`): win / DNB / double chance on the same side; over/under in the same direction at any line (totals and team totals); the same handicap side at any line. Contradictions are refused by the builder's existing `conflicts()` (now swap-aware). Different families (win vs over 2.5, BTTS) pass. The reason is stored in the candidate's `notes["cross_tier"]`.
- `MAX_PICKS_PER_EVENT` counts per tier under `different_outcome`; `forbid` keeps the old across-tier count and makes the Top's matches off-limits.
- Tests: the brief's six candidates (Arsenal to win / or draw / DNB rejected; Chelsea to win rejected; Over 2.5, BTTS accepted) in both orientations — including the home/away-swapped, differently-spelled event (`Chelsea FC v Arsenal FC`) — and the whole set under `forbid`.

**Phase 3 — three layers**
1. `select()` re-checks the final slate; a violating Daily leg is dropped, the Daily refilled with it excluded, `CROSS_TIER_DUPLICATE_REMOVED` counted and a WARNING logged with both legs. The Top is never touched (test forces the pre-filter to fail).
2. `picks.fixture_key` column (additive migration, backfilled from each row's own columns, legacy duplicates kept with a `#dup<id>` suffix so the index can always be built) and a unique index on `(pick_date, fixture_key, market, period, stat, entity, COALESCE(line, -999999), selection)` **without tier**. `store_picks` checks first (`has_fixture_selection`), skips with a WARNING, and also catches `IntegrityError`.
3. `summary_card` / `picks_message` log a WARNING if a Daily block repeats a Top block (same outcome, same two teams in either order); tests assert none appears in a real snapshot.

**Phase 4 — Top fallback** (`TOP_RELAXED_FALLBACK`)
Runs only when the strict search returns nothing: (1) odds band widened by `TOP_RELAX_BAND` each side (1.75–2.75), same probability floor; (2) legs within `TOP_RELAX_PROB` below `TOP_MIN_PROB` (0.58) join, still inside the widened band; (3) empty. Never fewer than 2 or more than 3 legs; the ≥ 2 sources, agreement and disagreement gates apply to the relaxed legs too. `diagnostics["top_relaxed"]` = `None | "band" | "prob"`, shown in `/lastrun` ("Top fallback") and the `picker:` log line. Tests: a strict-solvable pool gives an identical ticket with the fallback on or off; 1.62 × 1.62 = 2.62 → `band`; legs at 0.59–0.61 → `prob`; one eligible leg → none; legs at 0.55 never join.

## New environment variables

| Variable | Default |
|---|---|
| `CROSS_TIER_SAME_FIXTURE` | `different_outcome` (`forbid` = a Top match is off-limits to the Daily) |
| `TOP_RELAXED_FALLBACK` | `1` |
| `TOP_RELAX_BAND` | `0.25` |
| `TOP_RELAX_PROB` | `0.04` |

Rollback: `CROSS_TIER_SAME_FIXTURE=forbid`, `TOP_RELAXED_FALLBACK=0`.

## Phase 5 — live confirmation on a throwaway database

Isolation: `data/predbot.db` (the only DB here; the production file is on the container) copied to a temp path; `DB_PATH` pointed at the copy; `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, **`ODDS_API_KEY`** and `FOOTBALL_DATA_KEY` blanked (the Odds API key was blanked rather than left set); `--dry-run`. The driver `analysis/cross_tier_live.py` is `cli.main(["cli.py","picks","--dry-run"])` in-process with the scored pool captured. Run at 17:18 UTC, Saturday, MLB / MLS in the window (NBA out of season, so basketball is empty).

| Before / after | `data/predbot.db` | backup DB |
|---|---|---|
| prediction_cache rows / latest `updated_at` | 0 / none → 0 / none | 0 / none → 0 / none |
| picks / posts / deliveries / provider_cache | 0 / 0 / 0 / 0 → 0 / 0 / 0 / 0 | 25 / 0 / 2 / 0 → unchanged |
| file mtime | 12:39 (before the run) → unchanged | — |

The temp DB ended with 0 picks / posts / deliveries (dry run persists no snapshot) and 2 provider-cache rows; it was deleted afterwards.

**Provider calls spent (this task): 19 SharpAPI requests (budget 60), 37 SportsGameOdds objects (37 used this month; three 429s were retried by the client). Odds API: 0. No other network.** Run 2 spent **+0 / +0** (served from the copy's provider cache).

**1. Cards** (run 1; run 2 is identical):
```
🔥 TOP PICKS        Combined Odds: 2.01 · Total Picks: 2
  Football · MLS · DC United vs Charlotte FC — DC United or Charlotte FC @ 1.31
  Baseball · MLB · Los Angeles Angels vs Minnesota Twins — Los Angeles Angels +1.5 @ 1.53
📋 DAILY PICKS      Combined Odds: 9.27 · Total Picks: 8
  Tennis · WTA · Dabrowski / Stefani vs Y. Dang / X. You — Dabrowski / Stefani -1.5 Sets @ 1.24
  Football · MLS · Colorado Rapids vs Seattle Sounders FC — BTTS No (1st Half) @ 1.25
  Football · MLS · Nashville SC vs Chicago Fire — Under 3.5 Total Goals @ 1.61
  Baseball · MLB · Los Angeles Dodgers vs San Francisco Giants — Los Angeles Dodgers to Win @ 1.24
  Football · MLS · Real Salt Lake vs Vancouver Whitecaps — BTTS No (1st Half) @ 1.26
  Football · France • Ligue 1 · Angers vs ESTAC Troyes — ESTAC Troyes +1.5 @ 1.21
  Football · France • Ligue 1 · Toulouse vs Le Havre — Toulouse (DNB) @ 1.27
  Baseball · MLB · Cleveland Guardians vs Athletics — Cleveland Guardians to Win @ 1.54
```

**2. Cross-provider links formed — 15 SharpAPI ↔ SGO pairs, all MLB** (the two providers were *not* merged by the chain: 0 of 62 events carry two providers; the likely reason is the case-sensitive `league_id` check in `merge_events`, `mlb` vs `MLB` — not investigated or changed). All 15 were linked by the picker. Where the two providers spell the teams differently:

| SharpAPI | SGO | old `same_team` on both sides | new |
|---|---|---|---|
| `Tampa Bay Rays v BOS Red Sox` | `Tampa Bay Rays v Boston Red Sox` | linked | linked |
| `Texas Rangers v TOR Blue Jays` | `Texas Rangers v Toronto Blue Jays` | linked | linked |
| `ARI Diamondbacks v New York Yankees` | `Arizona Diamondbacks v New York Yankees` | linked (0.89) | linked |
| `Cleveland Guardians v Oakland Athletics` | `Cleveland Guardians v Athletics` | **not linked (0.69)** | **linked** |

So in this pool the old code would have missed 1 of 15 pairs (the Athletics game), and the other three abbreviated names scored 0.87-0.89 against the 0.82 threshold. The other 11 pairs have identical names. No home/away swap occurred in this pool (swaps are covered by unit tests only). The Angels game is one of the 15 pairs; its two views are `sharpapi` and `sportsgameodds` `Los Angeles Angels v Minnesota Twins`.

**3. Fixtures present in both tiers: 0.** The one candidate that would have created a duplicate was refused (item 4), so the final slate has no shared fixture; there were therefore no different-family pairs to display. This is the honest outcome of the rule working, but it means the "different outcome allowed" path was not exercised on live data today — it is covered by the unit tests only.

**4. Counts:** `CROSS_TIER_DUPLICATE = 1`, `CROSS_TIER_NESTED = 0`, **`CROSS_TIER_DUPLICATE_REMOVED = 0`** (the invariant caught nothing — the selection layer missed no case). The refused candidate, from the candidate's notes: SGO `Los Angeles Angels v Minnesota Twins` — `Los Angeles Angels +1.5` ← `CROSS_TIER_DUPLICATE: same tip as Top leg Los Angeles Angels +1.5` — the same bet the SharpAPI view put in the Top, offered again through the other provider: the exact duplicate the owner described, caught across providers.

**5. `top_relaxed` = `None`** (the strict search found the ticket: 4 tickets evaluated, 2.007).

**6. Repeat run** against the same temp DB, clock pinned to run 1's `now`, provider cache only: **+0 SharpAPI, +0 SGO; slate identical (10 picks vs 10)**.

## Notes and limits

- The old tests that pinned "one pick per match across both tiers" now run under `forbid` (`test_picker.py` ×2) or assert the per-tier form (`test_daily_ranking.py`); no test was deleted. Four test helpers that inserted identical picks under different provider ids now use distinct teams, because the same selection on the same match is exactly what the new unique index forbids.
- Not exercised live: a Top and Daily on the same match with **different** outcomes, a home/away swap, and `top_relaxed` (unit tests only).
- The fixture key is not a perfect cross-provider identity (unresolved competition on one side, a name that reduces differently, kickoffs straddling an hour boundary); the DB index is therefore a backstop behind the picker's fuzzy linking, not a replacement for it.
- Football name variants beyond `rcd`/`ud`/`sd` (`Espanyol`-type single-token aliases in other leagues) are still fuzzy-matched only; the 0.82 threshold was deliberately left alone.
- `merge_events` was not changed (out of scope): same-match events from different providers stay separate events; the picker links them, but their prices are not pooled.
