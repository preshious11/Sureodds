# BROADCAST_REPORT

Two broadcast messages, the Daily sized on its own, pages that turn in place. Six local commits (one per phase, the report in the last) on top of
`PRESENTATION_REPORT.md`, **not pushed**. `python -m pytest -q`: **981 passed**, green after every phase. No live
provider call, no real Telegram call, the production `DB_PATH` was never opened.

## Decisions you should look at first

1. **The Daily is now bigger, and Top × Daily is no longer bounded by 20.** On the replay day the Daily grows from 8 legs
   (own odds 9.54) to 10 legs (own odds **18.44**); with the Top (2.01) the whole slate multiplies to 37.0 where the old
   sizing capped it near 20. That is the point of the change (the band now describes the Daily alone), but it means
   `/lastrun` will show `with the Top: 37.02, old sizing` for one release.
2. **The channel post is also two messages with the four-button keyboard.** A keyboard on a channel post routes button
   presses to the bot with the channel as the chat; if you would rather the channel had no buttons, it is one argument
   in `daily_picks.run`.
3. **`DAILY_PICKS_TARGET` still caps Top + Daily legs together** (unchanged: "everything else about the Daily is
   unchanged"). With the Top at 2 legs the Daily can reach 18.
4. **`PHO` (Phoenix Suns) is in the alias table although it is not in your list** — it is the code SharpAPI actually sends
   for the Suns in `tests/fixtures/sharp_odds.json`; `PHX` is kept too.

## Phase 1 — the broadcast is two messages (`6605cd0`)

- `broadcast.picks_messages(snapshot, tz, now)` → `[(kind, text, keyboard)]`: the Top card (`picks_top`), then the Daily
  card (`picks_daily`), each with the Top / Daily / Stats / Yesterday keyboard. An empty tier still gets its message with
  the existing "No selection cleared the quality bar today." text; both empty → nothing. `picks_text` is kept as the two
  cards joined (for `/lastrun` and the dry-run print); what is *sent* is `picks_messages`.
- `deliver_picks` sends each subscriber their missing messages in order from one worker, stops at the first failure (a
  Daily never arrives before its Top), and records each success under its own kind. A retry sends only what is missing.
  Batching and the paced pool are unchanged; the 20 msg/s cap counts both messages (`interval × len(messages)`), and a
  record batch is `RECORD_BATCH` chats' worth of messages. `deliver_picks` still returns the number of subscribers served.
- Channel post (`jobs/daily_picks.run`): the same two messages, `posts.kind` `picks_top` / `picks_daily`, each posted only if
  not already recorded (`force` re-posts both), Top first, stop at the first failed send.
- Migration (`Database._migrate_split_picks_kind`, runs on every start, idempotent): each existing `deliveries` and `posts`
  row of kind `picks` is copied to `picks_top` and `picks_daily`, so nothing already sent is re-sent.
  `deliveries_exist("picks", day)` (used by the regenerate guard) now means "either message".
- `/start` sends only the menu (never a card), so there was no first-time delivery to split.
- Tests: `tests/test_two_messages.py` (11) — two `sendMessage` per subscriber in order with the four-button keyboard on
  both; a failed Daily is retried alone; a failed Top holds the Daily back; empty Top / empty Daily; pacing counts both;
  the channel post, its retry, and the migration. Older delivery tests were updated to two messages per chat.
- Files: `bot/broadcast.py`, `jobs/daily_picks.py`, `storage/db.py`, `tools/replay.py` (the harness counts distinct chats
  so the slate golden did not move), tests.

## Phase 2 — the Daily is sized on its own (`6d3e7f5`)

`picker.select`: the Daily's running product now starts at `1.0` (it started at the Top's odds), so
`DAILY_COMBINED_MIN_ODDS`–`MAX_ODDS` apply to the Daily's own legs. Untouched: the family ranking, one pick per match,
every cap, the cross-tier rule (Top legs are still consulted for duplicates, just not counted in the odds), the relaxed
pass, `DAILY_PICKS_TARGET`. New diagnostics: `daily_own_odds`, `daily_legs`, and `daily_odds_with_top` (one release, shown
by `/lastrun` as `own odds X (with the Top: Y, old sizing)`).

**Slate diff (`git diff 5.. golden_slate.json`, replay day 19 Sep):**

| | before | after |
|---|---|---|
| Top legs / odds | 2 / 2.007 | 2 / 2.007 — **identical** (`top` array equal, same order and prices) |
| Daily legs | 8 | **10** |
| Daily own odds | 9.54 | **18.44** (inside 10–20) |
| Top × Daily | 19.14 | 37.02 |

The old eight Daily legs are all still there; two were added, each at its slate position:
`Arizona Diamondbacks v New York Yankees — New York Yankees to Win (1st Half) @ 1.61` and
`Angers v ESTAC Troyes — ESTAC Troyes +1.5 @ 1.20`. Ranks 8–10 renumber accordingly in `daily` / `stored_picks`. The other
changed lines are `daily_by_league`, `daily_by_market`, `daily_family_picks`, `daily_ticket_by_sport` (counts including
the two added legs) and the three new diagnostic keys (`daily_legs 10`, `daily_own_odds 18.44`, `daily_odds_with_top 37.02`).
Nothing else in the slate moved (10 hunks, 139 insertions / 10 deletions).

Tests: `tests/test_daily_own_sizing.py` (9) — a Top of ~2.0 with 1.45-price legs: the Daily alone ≥ 10 (≥ 7 legs) and stops
before 20 on its own, where Top × Daily > 20 (the old rule stopped at ~9.5); six legs worth 9.29 are no longer enough; the
Top is identical whatever the Daily band; the relaxed pass waives caps but never the band; the pick cap still bounds the
legs; the diagnostics; `/lastrun`. Three older tests asserted the band on `ps.all()` (Top × Daily) or on "no win before all
handicaps are used up"; they now assert the Daily's own product and the ordering (edited, not deleted).

## Phase 3 — page turns edit in place (`VIEW_NAVIGATION`)

**Default is now `edit_pages`**: a page turn (◀ ▶) edits the message it was pressed on; a view switch (Top / Daily / Stats /
Yesterday, the `menu` button, and any button on a delivered card) sends a new message. `new` and `edit` remain. An unknown
value logs a warning and means `edit_pages`. `.env.example`, README and `config.py` updated.
Tests (`test_view_navigation.py`): the default resolves to `edit_pages`; a page turn on a delivered Daily message edits that
`message_id`; a page turn on an earlier day's message edits it and renders that day; every view switch sends.

## Phase 4 — no results line on picks cards

Removed the `📊 7d: … · ROI …` footer (`broadcast._record_line`), the dead `format.stats_line` and the `stats_line` argument of
`picks_card` / `picks_message`. Test: no delivered message, joined card, Top/Daily page or channel card contains `7d`, `ROI`,
`nW-nL`, `Yesterday`, a streak, or ✅ ❌ ➖ — with a snapshot whose stats would have produced the footer.

## Phase 5 — US city-code display aliases

`predbot/util/display_names.py`: `display_name(name, sport)` and `display_text(text, home, away, sport)`, a per-sport table
(`baseball`, `basketball`, `american_football`); a leading all-caps 2–3 letter code in the sport's table becomes its city,
anything else is returned unchanged. Applied only at render time: `pick_line_from_record` (match and outcome), the channel
`picks_message`, `yesterday_pick_line`, and the recap's best-pick line. Not in matching, `fixture_key`, storage or settlement
(a test asserts the record, the label and the key are untouched). Football is never touched (`LA Galaxy`, `DC United`,
`PSV Eindhoven`, `RCD Espanyol` render as they are). The recap is titled with the day it describes (`📊 RESULTS · Fri 18 Sep`).

**Codes verified against the repo's data**

| Confirmed | Where |
|---|---|
| `ARI Diamondbacks`, `TOR Blue Jays`, `BOS Red Sox` (baseball) | replay day's SharpAPI payload; `BOS` also `tests/fixtures/sharp_mlb_main.json` |
| `PHO Suns` (basketball) | `tests/fixtures/sharp_odds.json` |
| `TEX Rangers` (baseball) | `CROSS_TIER_REPORT.md` |

**Not confirmed against any fixture** (kept because they are the standard codes; a wrong one would only mis-name a card, and
football is unaffected): every other MLB code (ATL BAL CHC/CHI/CWS CIN CLE COL DET HOU KC LA/LAA/LAD MIA MIL MIN NY/NYM/NYY OAK
PHI PIT SD SEA SF STL TB WAS/WSH), every NBA code but `PHO` (incl. GS/GSW, NO/NOP, SA/SAS, LA/LAC/LAL, PHX, UTA, OKC), and **all
NFL codes** — the repo has no SharpAPI NFL fixture (only ESPN, which uses full names). I added a few codes you did not list
(`BKN CHA MEM` NBA; `BUF CAR GB JAX LV NE` NFL; `CHC CWS` MLB). `IND` is Indiana in basketball and Indianapolis in football.
**Ambiguity to know about:** a bare `LA` / `NY` / `CHI` is one city in every US table (`LA Lakers` → Los Angeles Lakers).

## Phase 6 — render golden

`golden_slate.json` is byte-identical to Phase 2's (`git diff` empty). `golden_render.json` re-recorded once. The replay
harness now records a chat's two messages, and the paging entries reflect `edit_pages`.

**The two broadcast messages** (before: one message — the Top section, a blank line, the Daily section with `Combined odds 9.54 · 8 picks`)

```
message 1 (252 characters, keyboard)          message 2 (1,076 characters, keyboard)
🔥 TOP PICKS · Sat 19 Sep                     📋 DAILY PICKS · Sat 19 Sep
🎟 Combined odds 2.01 · 2 picks · 🕘 …WAT     🎟 Combined odds 18.44 · 10 picks · 🕘 times in WAT

⚽ MLS · CF Montréal vs Columbus Crew         ⚾ MLB · Chicago White Sox vs Detroit Tigers
🎯 CF Montréal or Columbus Crew               🎯 Detroit Tigers +1.5
💰 1.33  ·  📊 73%  ·  🕘 12:30 AM            💰 1.51  ·  📊 64%  ·  🕘 7:10 PM
                                              … ten blocks, kickoff order, no results footer
⚽ MLS · St.Louis City vs Toronto FC
🎯 BTTS Yes
💰 1.51  ·  📊 64%  ·  🕘 1:30 AM
```

Keyboard on both: `🔥 Top Picks | 📋 Daily Picks` / `📊 Stats | 🗓 Yesterday`.

**Daily page 1 of 2** (5 blocks, sent as a new message by the Daily button) and **page 2 of 2** (edited in place by ▶, same
`message_id`; header unchanged):

```
📋 DAILY PICKS · Sat 19 Sep                   📋 DAILY PICKS · Sat 19 Sep
🎟 Combined odds 18.44 · 10 picks · …WAT      🎟 Combined odds 18.44 · 10 picks · …WAT
⚾ Chicago White Sox vs Detroit Tigers 7:10 PM ⚽ CF Montréal vs Columbus Crew (Over 2.5) 12:30 AM
⚽ Lyon vs Stade Rennes 7:45 PM                ⚾ Arizona Diamondbacks vs New York Yankees 1:10 AM
⚽ Angers vs ESTAC Troyes 7:45 PM              ⚽ Houston Dynamo vs FC Cincinnati 1:30 AM
⚽ Sevilla vs Barcelona 8:00 PM                ⚾ Los Angeles Dodgers vs San Francisco Giants 2:10 AM
🎾 Iva Jovic vs Peyton Stearns 11:00 PM        ⚽ Colorado Rapids vs Seattle Sounders FC 2:30 AM
Page 1 of 2                                   Page 2 of 2
```

(abridged to the match and time; the golden has the full blocks.)

**A pick line with an aliased name** (the replay day's games came through two providers, so their names were already full;
this is a SharpAPI-only game, from `test_display_names.py`):

```
before   ⚾ MLB · TOR Blue Jays vs BOS Red Sox        🎯 BOS Red Sox to Win
after    ⚾ MLB · Toronto Blue Jays vs Boston Red Sox  🎯 Boston Red Sox to Win
```

Card sizes: the largest replay card is now the Daily message at 1,076 characters (the delivered card was 1,114 as one
message); nothing is near 4096.

## Deployment notes

- **Migration:** on first start the `deliveries` and `posts` tables gain `picks_top` / `picks_daily` rows copied from every
  `picks` row (idempotent; the old rows stay). If the deploy happens after 09:30 the same day, nobody is re-sent that day's
  card. `deliveries_exist("picks", …)` keeps working for the "already delivered, don't regenerate" guard.
- **`VIEW_NAVIGATION`:** if it is set on the dashboard (the previous release made `new` the default, so a dashboard value may be `new`), remove
  it once this is deployed — the new default is `edit_pages`, and a leftover `new` would keep page turns sending messages.
- The Daily is larger from the first generation after the deploy; `/lastrun` shows `daily_legs`, own odds and (for this
  release only) Top × Daily under the old measure.
- Nothing to change for the alias table, the footer or the two messages.
- Not verified live: the two-message order under real Telegram latency, an edit of a delivered message, and any code in the
  alias table other than the five confirmed above.
