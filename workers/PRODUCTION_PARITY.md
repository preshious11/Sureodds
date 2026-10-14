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
3. **/stats and /yesterday**: ported the yesterday settled-results block (counts, hit rate, ROI, best win), the **by-sport breakdown**, and the **7-day trend** (settled-only hit rate/ROI, current win/loss streak, per-day strip of result marks, window capped to how long the engine has actually been running — `predbot/jobs/stats.py sports_day()`/`last_days()`/`_streak_of()`), all scoped to the correct tracked pool — **only predictions with a `top` or `daily` prediction_memberships row**, matching production's `predbot/storage/db.py picks_where(..., published_only=True)` exactly. All Predictions never contributes anywhere in this, including the by-sport/7-day additions (see `workers/tests/all_and_stats_rendering.test.mjs`: 100 All-Predictions-only rows proven not to move either section, a both-Top-and-Daily leg proven to count once in the by-sport breakdown too, the window-capping proven not to claim "Last 7 days" on an engine 2 days old). **Simplified, not ported**: the per-day strip's within-day ordering is kickoff time only (production additionally sorts Top legs before Daily within a day — needs a resolved-tier column this query doesn't carry); the 7-day Top-ticket hit-rate line (`top_ticket_7d`); the `/stats_top` / `/stats_daily` / `/stats_all` collection-specific sub-views and their `/results_<collection>` itemized settled-pick lists; the 30-day/all-time blocks. None of these fabricate anything — they're simply not computed yet.
4. **`/yesterday` currently renders the same aggregate block as `/stats`** rather than production's per-pick itemized list (✅/❌ per leg, grouped by Top/Daily tier, its own pagination). Documented simplification, not a redesign of what exists — no itemized listing existed in staging before either.
5. **Admin commands** `/run`, `/settle`, `/recap`, `/lastrun` are out of scope — staging has no generation/settlement subsystem to trigger from a command (that's GitHub Actions' job here). `/menu` **is now ported** (see below). Ticket Audit is disabled in production itself too (`ticket_audit_switch.ENABLED = False`), so there's nothing live to port there.
6. **Live match notifications** (`live_match_message`) — not a Telegram command, not ported; staging has no live-match push pipeline.
7. **Fixture grouping key** for /all uses `(home, away, start_utc)` rather than a canonical fixture/event id column; this is a close but not guaranteed-exact substitute if the same two teams play twice on the same date (not currently possible in the data, but noting the assumption).
8. **Per-subscriber timezone** is not modeled in the Worker's `subscribers` table in a way the views read — all times render in fixed Africa/Lagos (WAT), whereas production can localize to each subscriber's own saved timezone. Matches the Worker's existing (pre-port) behavior; not a regression, not newly introduced.

## Scheduling and group delivery (this pass)

Production posts Top/Daily and the settled recap to a single Telegram
**group** (`TELEGRAM_CHAT_ID`), never to individual subscribers
(`SUBSCRIBER_DELIVERY_ENABLED=false` in production's own `.env` — the
per-subscriber code path exists but is flag-disabled, not removed;
`predbot/jobs/daily_picks.py` / `recap.py`). The Worker previously had the
opposite shape: `TELEGRAM_CHAT_ID` was declared in `Env` but never read, and
the only broadcast loop (`sweepDeliveries` in `cron.ts`) sent to every row in
`subscribers` — correct for nothing production actually does today.

Added `postGroupPicks()` / `postGroupRecap()` (`workers/src/cron.ts`): single
send to `TELEGRAM_CHAT_ID`, gated by `POST_TIME`/`RECAP_TIME` and a COMPLETE
cache for the date, de-duplicated via the `posts` table's
`(kind, post_date)` unique key (`hasPosted`/`markPosted` in `db.ts` — these
already existed, unused until now). `handleScheduled()`'s `"post"` cron tag
now calls these; the `"recap"` tag now calls `postGroupRecap()` instead of
the old per-subscriber `sweepDeliveries(..., "recap")`, so individual
broadcasting is never invoked from the scheduler — matching production's
disabled state. `sweepDeliveries()` itself is untouched and still tested
(kept for a possible future explicit per-subscriber feature, same as
production keeps the code path behind its own flag).

**Still inert by design**: both gates from before remain — `STAGE3_CRON_ENABLED`
(absent → no-op) and the cron triggers in `wrangler.staging.toml` (commented
out). Flipping them is still a manual, explicit, reviewed step; nothing in
this pass changes that. New GitHub Actions workflow
`stage6-scheduled-staging.yml` adds the missing automatic daily trigger for
generation, but a schedule-triggered fire always calls
`stage5-acceptance.yml` with `dry_run=true`/`operation=fixture-replay`
(offline planning, zero provider calls, zero D1 writes) unless the repo
variable `STAGE6_LIVE_ENABLED` is set to `"true"` — a single Actions-settings
toggle, no code change, no redeploy. `allow_telegram_posts` stays `false` on
every scheduled call regardless, since Worker-side Telegram posting is a
separate, still-inert gate (above).

**Readiness-sweep fix (this pass, for the 09:00 Lagos race)**: generation
(GitHub Actions, stage6) and posting (this Worker) run on separate
infrastructure with no shared lock. A one-shot Cron fire at exactly 09:00
Lagos could not be trusted to find a COMPLETE cache if generation ran
long — and worse, the previous dispatcher matched on
`cron.includes("post")`/`"recap"`/`"deliver"`, which can never match a real
Cloudflare Cron Trigger pattern (plain cron syntax carries no descriptive
word), so once multiple `[[triggers.crons]]` entries were attached none of
them would have dispatched at all. Fixed: `handleScheduled()` now matches
the exact pattern via exported constants (`CRON_POST_SWEEP`,
`CRON_RECAP_SWEEP`, `CRON_DELIVER_SWEEP`, `CRON_WATCH`, in `cron.ts`), and
POST/RECAP are sweeps (fire every 5 minutes across a multi-hour window) —
`postGroupPicks`/`postGroupRecap`'s own COMPLETE-cache check and
`post_date` receipt make every repeat fire before and after the one real
send a safe no-op, so a slow or retried generation run still gets posted
exactly once instead of being silently skipped for the whole day. See
`workers/tests/cron.test.mjs`'s "readiness sweep" test for the exact race
this fixes.

## Custom Picks (this pass — partial port, not full parity)

Production's `predbot/custom_picks.py` + `custom_recovery.py` +
`custom_intent.py` is a private-chat, free-text request → beam-searched
ticket feature. Ported to `workers/src/custom_picks.ts` and wired into
`workers/src/index.ts`. **Faithful**: the four canned copy strings
(INTRO/UNAVAILABLE/TODAY_ONLY/HELP, verbatim), the candidate validation
rules (`normalizeCandidate` ~ `custom_picks.py normalize()` — same
cancelled/live/started/stale-evaluation/price-probability-range/
market-selection/line checks), the ticket text shape (`formatTicket` ~
`format_ticket()`, reusing the already-ported `combinedOdds`/`SPORT_ICON`),
and the access rules (private-chat only at every entry point; `/start
custom_picks` deep-link entry sends INTRO directly, bypassing the parser,
exactly like production; plain `/start`/`/stop` leave the session).

**Simplified, not faked** (every gap below fails safe — HELP/UNAVAILABLE/no-
ticket — rather than fabricating a pick or a ticket that violates the same
constraints production enforces):
1. **Request parsing** (`parseSimpleRequest`): recognizes explicit sport
   name/alias, "`N odds`"/"`odds of N`", and "`N picks/games/legs`" patterns
   only. Production's `custom_intent.py parse()` is a much larger NLP
   grammar (ambiguity detection, competition/market filters, conflicting-
   constraint detection, off-topic detection). Anything outside the
   recognized patterns gets HELP, never a guess.
2. **Ticket construction** (`buildTicket`): one leg per fixture, candidates
   tried in probability-descending order, kept if within the target-odds
   tolerance band (±10%, same `TOLERANCE` as production). This is a greedy
   selection, not production's per-fixture beam search with market-family
   diversity and odds-bucketed retention (`custom_picks.py search()`). It
   can return a worse (or no) ticket where production's search would find
   one, but it never returns a ticket that fails the same leg-count/odds
   checks production itself re-verifies (`valid_ticket`).
3. **No alternative-suggestion buttons on failure** — production's
   `prepare()`/`Recovery.ready` builds up to 3 tappable alternative requests
   when a ticket can't be built; not ported. A failure here is a plain text
   reply with no buttons.
4. **Session/rate-limit storage**: production holds "(user_id, chat_id) is
   in Custom Picks mode", conversation refinement context, and a
   token-bucket work budget + 30-requests/60s counter + 16-slot queue in
   plain in-memory dicts on one long-lived process
   (`predbot/custom_recovery.py`, `commands.py` `_custom_executor`). A
   Cloudflare Worker has no memory shared across invocations, so this port
   uses a new D1 table (`migrations/0006_custom_sessions.sql`,
   `custom_sessions`) for session membership, and a flat 3-second per-chat
   cooldown (`isCustomOnCooldown`/`recordCustomRequest`) in place of the
   token-bucket/counter/queue system. Tickets are still never persisted
   (same as production), only session membership and the cooldown
   timestamp are.
5. **Conversation refinement is not ported**: production merges a new
   message into a stored prior `Request` when classified as a
   "refinement" (e.g. "no, tennis instead"). Every message here is parsed
   independently; a follow-up message is a fresh request, not a refinement
   of the last one.
6. **No inline "✨ Custom Picks" button** added to the welcome/menu keyboard
   yet — the `/start custom_picks` deep-link entry point itself works
   (what the button would link to), but nothing in the Worker's own UI
   currently surfaces that link. Minor, cosmetic, not done this pass.

**Data contract (was missing, fixed this pass)**: the archive data Custom
Picks reads (`custom_pick_candidates`, `custom_pick_pools` — schema already
present in `migrations/0003_engine_state.sql`, matching production's
SQLite schema exactly) was never included in `stage5-acceptance.yml`'s
sync `--tables` list, so even though the D1 schema and the Worker's read
code now exist, no row would ever have reached staging D1 without this.
Added both tables to the `--tables` list (`d1_sync.py`'s `write_policy`/
`key_columns` already supported them structurally — this was a one-line
workflow config gap, not a missing schema or a missing sync-engine
feature).

## Group entry point correction (this pass)

The previous pass's Custom Picks port got the entry point wrong. Verified
directly against `predbot/bot/callbacks.py` and `predbot/jobs/daily_picks.py`:

- The "✨ Custom Picks" button is **never** in the private welcome/view-switch
  menu. It appears in exactly two places, both reusing the same
  `?start=custom_picks` deep link: (a) the group's automatic Top Picks post,
  but **only** when that day's Top is genuinely empty (`top_empty_markup`);
  (b) the group's community menu card, posted only via the admin `/menu`
  command (`community_menu_markup`). `telegram.ts`'s `customPicksButton()`/
  `topEmptyKeyboard()`/`communityMenuKeyboard()` now match this exactly;
  `topEmptyKeyboard()`/`communityMenuKeyboard()` take the bot's username and
  omit the button (never a dead link) if `TELEGRAM_BOT_USERNAME` isn't set,
  mirroring production's own `tg.get_bot_username()` guard.
- **`/menu` is now ported** (`index.ts`): admin-only (`TELEGRAM_ADMIN_IDS`,
  comma-separated numeric ids — new, matches `cfg.telegram_admin_ids`),
  posts the community menu to the configured group regardless of which
  chat `/menu` was typed in, exactly like `_post_community_menu`.
- **Found and fixed a second bug while correcting this**: the automatic
  group post (`cron.ts postGroupPicks`) was reusing the *paginated*
  interactive `/top`/`/daily` view. Production's automatic post
  (`predbot/bot/broadcast.py picks_messages`/`summary_card`) is always the
  **whole ticket in one message, never paginated** — a Daily ticket with
  more legs than `PICKS_PER_PAGE` (5) would have been posted to the group
  showing only its first page, with a stray "Page 1 of N" footer production
  never has. Added `renderGroupCard()` (`views.ts`) for this unpaginated
  path; `postGroupPicks`/`postGroupRecap` now also send with **no keyboard
  at all** except the genuinely-empty-Top case, matching
  `daily_picks.run`'s `markup = None` default exactly (the Worker
  previously always attached the interactive pager/switch keyboard to
  every automatic group post). See `workers/tests/group_posting_card.test.mjs`
  for the regression coverage of both bugs.

## Custom Picks — deep port (this pass, supersedes the earlier "partial port" section)

`workers/src/custom_picks.ts` was substantially rewritten from a greedy
pattern-matcher into a much closer algorithmic port:
- **`search()`**: the real bounded per-fixture beam search from
  `custom_picks.py` — per-fixture candidate grouping, market-family
  diversity in the option set, odds-bucketed beam retention when a target
  is given, capped expansions. Not a greedy "take the best N" anymore.
- **`parseRequest()`**: a much fuller `custom_intent.py` port — league/
  competition detection (see below), sport aliases with "no/without X"
  negation rejection, "all sports", an odds target OR a pick count with
  conflicting-number rejection, market include/exclude scanned in TEXT
  order with negation-carry-over (not object-iteration order — this was
  a real bug caught and fixed mid-implementation, see
  `workers/tests/custom_picks.test.mjs`), preference qualifiers, and
  refinement/quality-upgrade intent detection.
- **`refine()`**: conversation refinement — a follow-up message classified
  as a refinement merges only its newly-specified fields into the stored
  previous request (excluded markets accumulate rather than replace,
  matching `custom_recovery.py refine()` exactly).
- **`failureReason()` + `prepareAlternatives()`**: honest, non-generic
  failure copy plus up to 3 *proven* alternative tickets (each
  independently re-validated before being offered — never a label
  promising a ticket that doesn't exist).
- **Suggestion tokens are now real and durable**: `migrations/
  0007_custom_picks_conversation.sql` adds `custom_suggestions` (replaces
  production's in-memory `Recovery.ready` dict, which can't survive a
  Worker's statelessness) and a `request_json`/`request_expires_at`
  column on `custom_sessions` (replaces `Recovery.conversations`). A
  button tap (`cp|<token>` callback, handled in `index.ts`) is
  ownership-checked, re-validated, and — if the specific token has
  expired — offers whichever other suggestions from the same prepare()
  call are still valid, exactly like production, rather than a dead end.

**Still simplified, not faked** (every gap fails to HELP/a generic
alternative, never a wrong answer): the league/competition catalog
(`LEAGUES` in custom_picks.ts) is a curated ~10-entry subset of
production's full `BY_ID` catalog (~100 competitions with per-competition
sport mapping) — an unrecognized league name falls through the same way
an unrecognized one does in production. No fuzzy typo-correction pass
(`custom_intent.py normalize_text`'s `difflib` step). The alternatives
ladder covers the three most common failure shapes (relax pick count,
step the odds-target ladder, drop all filters), not production's fuller
per-market/per-sport branching. `claimed_count`/button-label richness is
a reasonable equivalent, not byte-identical to production's `label_for`.

## Stats: 30-day/all-time blocks, collection views, itemized /yesterday (this pass)

Closing the remaining documented stats gaps:
- **30-day and all-time blocks** appended to `/stats` (not `/yesterday`),
  same `_block()` shape as the existing yesterday card, over wider
  `getStatsRowsForRange()` windows. Same Top/Daily-only scoping, proven
  unaffected by All-Predictions-only rows in
  `workers/tests/all_and_stats_rendering.test.mjs`.
- **`/stats_top` and `/stats_daily`** (new commands): the yesterday
  aggregate block scoped to one collection only
  (`getStatsRowsForDateByCollection`). The by-sport/7-day/30-day/all-time
  sections are NOT included on these two views yet (kept to a single
  aggregate, documented simplification, not fabricated) — `/stats_all`
  and the `/results_<collection>` itemized sub-lists remain unported.
- **`/yesterday` is now itemized**: one ✅/❌/➖/↩️/❔ line per tracked leg,
  Top legs listed before Daily (`getItemizedStatsRowsForDate`, tier
  resolved with the same top-priority-over-daily rule as everywhere
  else), with still-pending legs called out separately rather than
  silently included or excluded. `/stats` keeps the aggregate-only view
  (counts/hit-rate/ROI/by-sport/7-day/30-day/all-time), matching
  production's own `/stats` vs `/yesterday` split.
