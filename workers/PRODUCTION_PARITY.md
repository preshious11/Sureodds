# Staging Worker vs. production bot — formatting/UI parity

Source of truth for production: `predictions-bot/predbot/bot/format.py`,
`pagination.py`, `callbacks.py`, `telegram.py`. Ported into
`workers/src/format.ts`, `telegram.ts`, `views.ts`. Golden cross-check:
`workers/tests/fixtures/golden_pick_line.json`, rendered independently by
`predictions-bot/tests/test_bot_format_golden.py` and
`workers/tests/format.test.mjs` — both must produce byte-identical text.

## Ported this pass (parity achieved)

| Area | Production | Staging before | Staging now |
|---|---|---|---|
| Pick line (3-line block) | icon+league+match / 🎯 outcome / 💰 price·📊 confidence%·🕘 time | one line, no league/confidence/time | matches (see `format.ts: pickLines`) |
| Combined odds | multiplied, ticket header, whole-ticket even mid-pagination | none | matches (`combinedOdds`, `cardHeader`) |
| /top, /daily empty states | "No Top Picks for today.\n\nSee other picks:" / "No selections qualified today." | generic "honest empty" text | matches |
| /top, /daily pagination | pager row `◀️ N/M ▶️` (no-op middle button), omits ◀️/▶️ at bounds | unbounded "Next ▶" always shown | matches |
| View-switch keyboard | `[All], [Top,Daily], [Stats,Yesterday]`, no active-view highlight | 2×2+1 layout with `• label •` highlight | matches |
| /start welcome keyboard | same 3 rows as the switch keyboard | 2×2+1 layout | matches |
| Empty /top keyboard swap | `[Daily, All]` (+ Custom Picks, not ported) | n/a | `[Daily, All]` |
| /all | 2-step: sport menu (counts, buttons) → per-sport fixture pages, fixture never split across a page | flat list, no sport grouping | matches (menu + fixture pages + "◀️ Sports" back button) |
| /stats, /yesterday | yesterday's settled won/lost/push/void/unresolved, hit rate, ROI, best win | opaque `stats_text` JSON blob (always "settlement summary pending" in practice) | real computation from `picks` (see below) |
| callback_data codec | `pg\|view\|page[\|yyyymmdd]` | already identical | unchanged, confirmed compatible |

## Data trace (deliverable #6)

No D1 schema change was needed. Every field the ported formatting uses was
already present:
- Pick line: `picks.sport, league, home, away, label, price, prob, start_utc` — all existing columns. `label` is the engine's own pre-formatted outcome text (same column the production bot itself displays), so no market-code-to-English translation needed reimplementing.
- Combined odds / ticket size: computed from `picks.price` across the full collection (`getAllPicksForCollection`, new query, no new column).
- /all sport grouping: `picks.sport` + `(home, away, start_utc)` as the fixture key (no separate fixture-id column exists; this is a reasonable substitute, see Known differences).
- Stats: `picks.status, price, result_json` — same columns `predbot/jobs/stats.py` reads from the same table.

## Known differences (not fully ported — flagging per deliverable #10)

1. **Country prefix on league** (`"England • Premier League"`) — `picks` has no `country` column. Pick lines show the league alone. Confirmed via the shared golden fixture that output is byte-identical to production for the country-absent case; a country column would be the smallest necessary schema addition to close this, not done here per "smallest necessary change" and because it's cosmetic.
2. **Live started/finished marker** (`🔴` / `· Finished`) — ported using production's exact `DURATION_H` table, but this is a best-effort estimate on both sides already (not a live feed), so there's nothing to further verify against.
3. **/stats and /yesterday**: ported the yesterday settled-results block (counts, hit rate, ROI, best win) exactly, scoped to the correct tracked pool — **only predictions with a `top` or `daily` prediction_memberships row**, matching production's `predbot/storage/db.py picks_where(..., published_only=True)` exactly (derived tier: `top` if the row has a `top` membership, else `daily`, else `all`; a row is kept only when that derived tier isn't `all`; since this is one row per prediction id, a leg published into both Top and Daily is never double-counted). All Predictions never contributes. (An earlier version of this port incorrectly scoped these two views to the whole `all` collection — fixed; see `workers/tests/all_and_stats_rendering.test.mjs` for the regression coverage: 100 All-Predictions-only rows proven not to move the numbers, a both-Top-and-Daily leg proven to count once, and the pending/void settlement gates proven to match `day_block`.) **Not ported**: the 7-day trend section, per-day result strip, win streak, by-sport breakdown, and the `/stats_top` / `/stats_daily` / `/stats_all` collection-specific sub-views and their `/results_<collection>` itemized settled-pick lists. These need multi-day aggregation queries and were out of scope for this pass's budget — current output is correct and non-fake, just narrower than production's full stats surface.
4. **`/yesterday` currently renders the same aggregate block as `/stats`** rather than production's per-pick itemized list (✅/❌ per leg, grouped by Top/Daily tier, its own pagination). Documented simplification, not a redesign of what exists — no itemized listing existed in staging before either.
5. **Deep-link features** (`✨ Custom Picks`, `🔍 Ticket Audit`) and **admin commands** (`/run`, `/settle`, `/recap`, `/lastrun`, `/menu`) are out of scope — staging has no equivalent subsystem and none was invented.
6. **Live match notifications** (`live_match_message`) — not a Telegram command, not ported; staging has no live-match push pipeline.
7. **Fixture grouping key** for /all uses `(home, away, start_utc)` rather than a canonical fixture/event id column; this is a close but not guaranteed-exact substitute if the same two teams play twice on the same date (not currently possible in the data, but noting the assumption).
8. **Per-subscriber timezone** is not modeled in the Worker's `subscribers` table in a way the views read — all times render in fixed Africa/Lagos (WAT), whereas production can localize to each subscriber's own saved timezone. Matches the Worker's existing (pre-port) behavior; not a regression, not newly introduced.
