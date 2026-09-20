# PRESENTATION_REPORT

Messages that stay, a truthful stats card, cleaner picks cards. Seven local commits (six phases plus this report) on top of `FINAL_REPORT.md`
(`5daa2f0` … the report commit), **not pushed**. `python -m pytest -q`: **925 passed**, green after every phase. No live provider
call, no real Telegram call, the production `DB_PATH` was never opened (the replay harness works on a temp copy).

## Decisions you should look at first

1. **`/daily` now shows the Daily ticket only.** Until now the Daily view was Top + Daily (10 picks, one combined
   19.14) while the delivered card showed Daily separately (8 picks, 9.54). "Each card shows its own odds and its own
   count" cannot hold with the union, so `DailySnapshot.view_items("daily")` returns `daily_picks`. The Top legs are
   no longer repeated in `/daily`. The slate, the stored picks and the delivered card's contents are unchanged.
2. **💹 (ROI) is not in the Phase 3 emoji table but is in the Phase 2 stats template.** I followed the template
   ("exactly this shape"): the stats card uses 📊 🗓 🏅 📈 🎟 🏆 🔥 ✅ ❌ ➖ ↩️ 🎯 💹 plus the sport icons. If 💹 must go,
   it is one string in `format.yesterday_lines`.
3. **Two things Phase 1 asked for were finished in Phase 3:** the date in every card header (`· Sat 19 Sep`) is part of
   the new header, so it lands with the card layout.
4. **`golden_slate.json` was built from the old golden minus one volatile key.** `api_usage.<provider>.today` reads
   the real calendar day (not the pinned clock), so the old golden had already started failing the day after it was
   recorded (`19`/`37` became `0`). Phase 0 strips the key (`_VOLATILE`); every other byte of the slate is identical to
   the old golden (checked key by key). That is the only edit to the slate outside Phase 4.

## Phase 0 — golden split (`5daa2f0`)

`golden.json` → `golden_slate.json` (status, date, Top/Daily picks in slate order with prices and labels, stored
picks, delivery counts, diagnostics minus timings) and `golden_render.json` (every reader-facing text).
`tests/test_replay_golden.py` asserts both plus the offline/cross-tier checks. Files: `tools/replay.py`,
`tests/test_replay_golden.py`, `tests/fixtures/replay/golden_{slate,render}.json`.

## Phase 1 — messages that stay (`9053759`)

- **`VIEW_NAVIGATION` = `new` (default) | `edit_pages` | `edit`** (`.env.example`, README; an unknown value logs a
  warning and means `new`).
  `new`: every press — view switch or page turn — sends a new message with its own keyboard; nothing is edited or
  deleted. `edit_pages`: switches send, page turns edit the same `message_id`. `edit`: today's call sequence (the
  toast now says `Daily Picks · page 2/5`; the old one carried the 📋 emoji).
- Page-turn buttons carry their card's day: `pg|daily|1|20260920`. A view-switch button carries none (`pg|daily|0`)
  and opens the current day. A press with a day renders that day's picks via the new
  `UnifiedFetcher.snapshot_for(day)` (today's cache, or the picks table for an older day; never a provider call).
  Old-format payloads (`pg|daily|1`) still parse and count as a view switch.
- Every press is answered (`answerCallbackQuery`): `Daily Picks ↓` for a switch, `Page 2 of 3` for a turn.
- `/start` is untouched; the welcome message (still 🏆, three commands) stays in the chat.
- Tests: `tests/test_view_navigation.py` (all three modes, the default, an old message's page turn). The 800+
  older tests that fake only `edit_message_text` for buttons keep running under `edit` through an autouse fixture in
  `conftest.py` (the replay golden and `test_view_navigation` are exempt and run on the shipped default).
- Files: `config.py`, `callbacks.py`, `pagination.py`, `cache/fetcher.py`, `.env.example`, tests.

## Phase 2 — a truthful stats card and the audit trail (`cea8532`)

- `jobs/stats.py::compute` now returns, keyed on the reader's local dates: `yesterday` (`issued won lost void push
  settled final`, hit rate and ROI only when `final`, `best_win` with `label outcome home away price`, ties broken on
  `prob`), `by_sport_yesterday` (a sport with picks but nothing settled is present with `settled = 0`), `last7`
  (settled only, hit rate, streak `W3`/`L2`, per-day strip most-recent-first in listing order, days with nothing
  settled omitted) and `top_ticket_7d` (only days whose Top legs are all settled; a ticket hits when no leg lost and
  one won). The existing keys (`last30`, `all`, …) are still there.
- `format.stats_message` / `recap_message` render through the same helpers (`yesterday_lines`, `by_sport_lines`,
  `last_days_lines`, `counts_line`); the recap differs only by its title. `/yesterday` is now a view (it used to be
  the recap text): every pick of the day, Top legs first under 🔥, Daily under 📋, in the cards' kickoff order,
  paginated with `PAGE_SIZE`, an unsettled pick with no mark; its header `10 picks · ✅ 6 ❌ 3 ➖ 1` comes from the
  same `day_block` as the stats numbers. A **Yesterday** button joined the keyboards (two rows of two).
- Judgement calls: per-sport `won/settled` (the example's denominators add up to the day's picks, void included;
  the hit rate is `won/(won+lost)`); `Last 1 day` in the singular; a losing streak prints `L2` without 🔥; the Top
  ticket line reads `of the last 7 days` only when seven days counted, otherwise `of 3 settled days`; an older
  snapshot's stats (no `issued`) render as settled and say nothing about yesterday rather than "no picks were issued".
- Tests: `tests/test_stats_card.py` (27) over a synthetic DB — final day, non-final day with a sport at `—`, a day
  with no picks, a Top ticket that hit and one that missed, every empty state, the marks in `/yesterday` against the
  card, the recap using the same helpers, the emoji set. `test_recap_format.py` was rewritten for the new card.
- Files: `jobs/stats.py`, `jobs/recap.py`, `bot/format.py`, `bot/pagination.py`, `bot/callbacks.py`,
  `bot/commands.py`, `bot/broadcast.py`, `cache/models.py`.

## Phase 3 — picks cards (`eaec052`)

- Removed `Generated …` and `odds as of …`. Header `🔥 TOP PICKS · Sun 20 Sep` / `🎟 Combined odds 2.01 · 2 picks · 🕘
  times in WAT`; pick block `⚽ MLS · DC United vs Charlotte FC` / `🎯 …` / `💰 1.31  ·  📊 76%  ·  🕘 12:30 AM`; a match
  under way gets ` 🔴`; paginated pages end with `Page 2 of 3`.
- Kickoff order everywhere, at render time only (`format.by_kickoff`, stable, applied before paging).
- Own numbers per card: the header's odds and count price the whole ticket on every page (the old page header counted
  the slice). The delivered/channel card is the Top card followed by the Daily card; the old `📊 TODAY'S RESULTS` title
  is gone (the Top header carries the date).
- Reader's zone: `zone_label(tz, day)` = `tzname()` at noon of the card's date (`WAT`, `BST`, `EDT`), the IANA name
  when the platform returns a numeric name. Never a literal `WAT`.
  **Known edge:** on a DST-change day one label covers the whole card, so a 1:59 AM kickoff that is still EST reads
  under `times in EDT` (pinned in `test_prediction_timezone.py`).
- Delivery: `telegram._split` cuts only at a blank line between blocks, keeps a ticket header with its first block,
  and never inside an HTML tag or entity.
- Emoji: help text lost 👋 🛑 🌍 🛠; toasts have none; buttons are `🔥 Top Picks` (was 🏆). `🏁 Finished` became the text
  ` · Finished` (no vocabulary emoji). The `/start` text is unchanged. Command replies for `/timezone` and `/stop`
  keep their ✅/🌍/🛑 (not a card, toast or help text).
- Tests: `tests/test_cards.py` (22) plus the rewritten card assertions in about a dozen older files (nothing deleted;
  tests pinning the old layout now pin the new one).
- Files: `bot/format.py`, `bot/pagination.py`, `bot/telegram.py`, `bot/broadcast.py`, `bot/callbacks.py`,
  `bot/commands.py`, `cache/models.py`.

## Phase 4 — full names and the command menu (`e7d715b`)

- `chain.merge_events` keeps, per side, the fuller of the two providers' names (`_fuller`: more words, then longer;
  always one of the two spellings; compared side-for-side when the second provider lists the teams the other way
  round). Tests use the pairs from `CROSS_TIER_REPORT.md`: `BOS Red Sox`→`Boston Red Sox`,
  `TOR Blue Jays`→`Toronto Blue Jays`, `Athletics`→`Oakland Athletics`, `TEX Rangers`/`Boston…` swapped, and
  `ARI Diamondbacks`→`Arizona Diamondbacks` (from the replay day).
- `setMyCommands` (`/top /daily /stats /yesterday /timezone /help`) is sent when the command loop starts, after it
  holds the polling lease, on every (re)start; failure is logged, never fatal.
- **Slate re-record, the only one after Phase 0** — `git diff 5daa2f0 e7d715b -- golden_slate.json`:

```
@@ -391 +391 @@
-    "kept": "sharpapi: Tampa Bay Rays v BOS Red Sox",
+    "kept": "sharpapi: Tampa Bay Rays v Boston Red Sox",
@@ -419 +419 @@
-    "kept": "sharpapi: Texas Rangers v TOR Blue Jays",
+    "kept": "sharpapi: Texas Rangers v Toronto Blue Jays",
@@ -440 +440 @@
-    "kept": "sharpapi: ARI Diamondbacks v New York Yankees",
+    "kept": "sharpapi: Arizona Diamondbacks v New York Yankees",
```

  Three substitutions in the `merged_pairs` diagnostic and nothing else: same picks, same order, same prices.
  None of the replay day's picks is on those three games, so no pick's text changed in the recorded slate; the
  Athletics case is covered by the unit test. **Live note:** `fixture_key` reduces names to a canonical form, so
  today's slate is unaffected; a re-generation on the deploy day could key a merged game differently from rows stored
  earlier that day (the picks table's unique index would then not see them as the same fixture).
- Files: `providers/chain.py`, `bot/telegram.py`, `bot/callbacks.py`, `bot/commands.py`, tests.

## Phase 5 — render golden (`3405cb2`)

`tools/replay.py` now renders every card: real callback data for the page turns (the old harness sent `top:0`, which
is not a callback, so `pages` had only recorded the acknowledgement), the delivered cards per zone with how they
split, and — over a settled history cloned from the recorded slate onto the four previous days — `/stats` (final and
partial), the recap, `/yesterday` (both states), the Stats/Yesterday buttons and every empty state.
`golden_render.json` re-recorded once (my first recording had four spaces before an unsettled `/yesterday` line where
the brief shows three; fixed and re-recorded before commit, so the history holds one render re-record).
`golden_slate.json` is byte-identical to `e7d715b`.

**Confirmation:** `golden_slate.json` changed in exactly two commits — Phase 0 (created from the old golden, minus
the volatile `today` counter, see Decision 4) and Phase 4 (the three team-name substitutions above).

### Before / after (replay day 19 Sep 2026, Lagos reader)

Before = the code at `5daa2f0`; the stats/recap "before" is rendered over the same seeded history.

**Top card**

```
BEFORE                                         AFTER
🏆 Top Picks for Today 2026-09-19              🔥 TOP PICKS · Sat 19 Sep
                                               🎟 Combined odds 2.01 · 2 picks · 🕘 times in WAT
Football
MLS                                            ⚽ MLS · CF Montréal vs Columbus Crew
CF Montréal vs Columbus Crew                   🎯 CF Montréal or Columbus Crew
<b>Prediction:</b> CF Montréal or Columbus Crew 💰 1.33  ·  📊 73%  ·  🕘 12:30 AM
Odds: 1.33
Confidence level: <b>73%</b> | 12:30 AM WAT   ⚽ MLS · St.Louis City vs Toronto FC
                                               🎯 BTTS Yes
Football                                       💰 1.51  ·  📊 64%  ·  🕘 1:30 AM
MLS
St.Louis City vs Toronto FC
<b>Prediction:</b> BTTS Yes
Odds: 1.51
Confidence level: <b>64%</b> | 1:30 AM WAT

Total Picks: 2
Combined Odds: 2.01 · odds as of 6:39 PM WAT
<i>Generated 6:39 PM WAT</i>
```

**Daily, page 2 of 2** — before, page 1 read `Total Picks: 5 / Combined Odds: 19.14` (Top + Daily, the page's own
five), page 2 `Total Picks: 5`, and a footer `<i>Page 1/2</i>`. After:

```
📋 DAILY PICKS · Sat 19 Sep
🎟 Combined odds 9.54 · 8 picks · 🕘 times in WAT

⚽ MLS · Houston Dynamo vs FC Cincinnati
🎯 BTTS No (1st Half)
💰 1.27  ·  📊 75%  ·  🕘 1:30 AM

⚾ MLB · Los Angeles Dodgers vs San Francisco Giants
🎯 Los Angeles Dodgers to Win (1st Half)
💰 1.25  ·  📊 76%  ·  🕘 2:10 AM

⚽ MLS · Colorado Rapids vs Seattle Sounders FC
🎯 BTTS No (1st Half)
💰 1.24  ·  📊 76%  ·  🕘 2:30 AM

Page 2 of 2
```

**/stats, final day**

```
BEFORE                                         AFTER
📊 SUREODDS RESULTS — YESTERDAY                📊 STATS · Sat 19 Sep

✅ Wins: 6                                     🗓 Yesterday · Fri 18 Sep · 10 picks
❌ Losses: 3                                   ✅ 6  ❌ 3  ➖ 1
➖ Void: 1                                     🎯 67%  ·  💹 ROI -13%
                                               🏆 Best: Detroit Tigers +1.5 · Chicago White Sox vs Detroit Tigers · @1.51
🎯 Hit Rate: 66.7%
                                               🏅 By sport
⚽ Football: 4/6                                ⚽ Football 4/7  ·  ⚾ Baseball 2/2  ·  🎾 Tennis 0/1
⚾ Baseball: 2/2
🎾 Tennis: 0/1                                  📈 Last 4 days
                                               ✅ 24  ❌ 12  ➖ 4  ·  🎯 67%  ·  🔥 W2
📈 LAST 7 DAYS                                 Fri ✅❌✅✅✅❌❌✅✅➖  Thu ❌✅✅✅✅✅✅❌➖❌  Wed …  Tue …
Wins: 24
Losses: 12                                     🎟 Top ticket hit 1 of 4 settled days
Hit Rate: 66.7%

🔥 BEST PICK
Detroit Tigers +1.5 @ 1.51 ✅
```

**/stats, day not final** — the case from your screenshot: before, a 60.0% hit rate over only the settled picks, no
Baseball, a best pick with no teams. After:

```
📊 STATS · Sat 19 Sep

🗓 Yesterday · Fri 18 Sep · 10 picks
✅ 3  ❌ 2  ➖ 1
6 of 10 settled · final card after the last result
🏆 Best so far: CF Montréal or Columbus Crew · CF Montréal vs Columbus Crew · @1.33

🏅 By sport
⚽ Football 3/5  ·  ⚾ Baseball —  ·  🎾 Tennis 0/1

📈 Last 4 days
✅ 21  ❌ 11  ➖ 4  ·  🎯 66%  ·  🔥 W1
Fri ✅✅❌❌✅➖  Thu …  Wed …  Tue …

🎟 Top ticket hit 1 of 3 settled days
```

**Recap** (channel post and delivered) — identical body to `/stats`, title `📊 RESULTS · Sat 19 Sep`; before it was
the old `📊 SUREODDS — YESTERDAY` block shown under /stats above.

**/yesterday, page 1 of 2** (before: the recap text, no per-pick list). Final day, then the partial day:

```
🗓 YESTERDAY · Fri 18 Sep                      🗓 YESTERDAY · Fri 18 Sep
10 picks · ✅ 6 ❌ 3 ➖ 1                        10 picks · ✅ 3 ❌ 2 ➖ 1

🔥 Top                                         🔥 Top
✅ 🎯 CF Montréal or Columbus Crew · … @1.33    ✅ 🎯 CF Montréal or Columbus Crew · … @1.33
❌ 🎯 BTTS Yes · St.Louis City vs Toronto FC …      🎯 BTTS Yes · St.Louis City vs Toronto FC · @1.51

📋 Daily                                       📋 Daily
✅ 🎯 Detroit Tigers +1.5 · … @1.51                 🎯 Detroit Tigers +1.5 · … @1.51
✅ 🎯 Stade Rennes +1.5 · Lyon vs … @1.20      ✅ 🎯 Stade Rennes +1.5 · Lyon vs … @1.20
✅ 🎯 Barcelona to Win · Sevilla vs … @1.25         🎯 Barcelona to Win · Sevilla vs … @1.25

Page 1 of 2                                    Page 1 of 2
```

### Every empty state (`golden_render.json`)

```
before POST_TIME      📋 Today's picks are ready at 09:00 WAT.
generating            📋 Today's picks are being generated — try again in a minute.
assembling (PARTIAL)  📋 <b>Today's predictions are still being assembled</b> - a data source did not answer. …
Top empty             🔥 TOP PICKS · Sat 19 Sep / No selection cleared the quality bar today.
Daily empty           📋 DAILY PICKS · Sat 19 Sep / No selection cleared the quality bar today.
no results ever       📊 No results yet — the first recap arrives the morning after the first slate.
no picks yesterday    🗓 Yesterday · Fri 18 Sep — no picks were issued.        (/yesterday: 🗓 YESTERDAY · … / No picks were issued.)
nothing settled yet   🗓 Yesterday · Fri 18 Sep · 10 picks — results come in after the last match ends.
fewer than 7 days     📈 Last 2 days      (and "Last 1 day")
```

## Card sizes

Nothing comes near 4096. The longest reader-facing text in the replay day is a delivered card of **1,114** characters
(`PAGE_SIZE` = 5; a Daily page is ≤ 556). With deliberately absurd labels (a 120-character outcome, a 55-character
competition, 45-character team names) a page of 5 is **1,623** characters, and one block is 382. A 29-pick card of
such picks (9 Top + 20 Daily, 9,170 characters) is delivered as three messages of 3,871 / 3,736 / 1,559, cut only
between blocks (tested). No card comes within 300 characters of the limit at any page size the bot uses.

## Not verified

- Real Telegram rendering: emoji widths (the 3-space indent under an unsettled `/yesterday` line assumes ✅ + space
  is about three columns), `↩️`, and how the two-row keyboard wraps on a narrow phone.
- A live `setMyCommands` call and a live button press (the fake client and a stubbed HTTP session only).
- The Athletics/`Oakland` case on real data (no pick on it in the replay day).
