# predbot — multi-sport predictions Telegram bot

A Telegram bot for Nigerian SportyBet punters. It pulls odds from The Odds API,
SharpAPI and SportsGameOdds, prices the markets it can settle with its own model,
and posts a daily **Top ticket (~2.00 combined odds) + a Daily ticket**. It settles
results automatically (football-data.org, ESPN, EuroLeague, SportsGameOdds, The Odds API scores) and
tracks a rolling record.

It is written in pure Python 3.11 (only `requests` is required) and runs as a single
worker process (a Docker container; the root `Dockerfile` runs `python main.py`).

> **Honest note first.** No model can guarantee betting results — sports are
> genuinely uncertain and every book builds in a margin. This bot estimates
> probabilities and expected value and is transparent about how; it is a
> research/automation tool, not a money printer. Treat "top picks" as *the
> model's most confident estimates*, not sure things. Bet responsibly, only what
> you can afford to lose, and check the gambling laws where you live.

---

## What it does

- **Provider chain.** Providers are queried in `PROVIDER_ORDER` (default
  `oddsapi,sharpapi,sportsgameodds`) and their events are merged (fuzzy team-name +
  kickoff match), so a pick can be backed by several books. Only `ALLOWED_SPORTS`
  (football, basketball, American football, baseball, tennis) are kept; anything
  else a provider returns is dropped and counted as `sport_rejected`. SharpAPI is
  required; the others are optional. A line is priced at the **median** of the books
  that quote it (`PRICE_MODE=median`, or `best` for the highest price + bookmaker).
- **Full market coverage.** Not just win/lose — see the list below.
- **A real pricing model** (not just the odds read back at you) — see "How the
  model works".
- **Predictions first, packaging afterwards.** The model produces one eligible representative per match **and market
  family** (1X2 / Moneyline, Double Chance, Draw No Bet, Handicap, Over/Under, BTTS) and every one is stored
  (**All Predictions**). **Top Picks** (~2.00 combined odds as a *target*) and **Daily Picks**
  are then selected from that stored pool; neither can remove a prediction, and either can legitimately be small
  or empty. See "Architecture" below.
- **Only picks that can be settled.** A candidate is accepted only if some provider
  can later return its result (see "Settlement"); otherwise it is rejected as
  `NO_RESULTS_PATH` and counted per league in the generation diagnostics.
- **Auto-settlement + stats.** It posts a daily recap (each pick ✅/❌, win–loss,
  best win, 7-day ROI) and a rolling 7-day record.

---

## Market coverage

Every market below is parsed into a single canonical form, priced by the model,
and graded on settlement:

| Group | Markets |
|-------|---------|
| Result | 1X2 (3-way), Moneyline (2-way), Draw No Bet, Double Chance |
| Totals | Over/Under (match, team, and per-stat: goals/points, corners, cards, games, sets), including Asian quarter lines |
| Handicap | Asian & European handicaps (incl. quarter lines), on goals/points/corners/cards |
| Goals | Both Teams To Score, Team To Score, Clean Sheet, Win To Nil |
| Combos | HT/FT, Result & Over/Under, Result & BTTS, Winning Margin, Correct Score |
| Other | Odd/Even (match & team) — priced and settled, but not selected by default |
| Props | Player props (**only via SportsGameOdds**), off by default (`MAX_PLAYER_PROPS=0`) |

Only the seven families above enter All Predictions. `DISABLED_MARKETS` (default `odd_even,prop,winning_margin,ht_ft`) lists market ids
the Top and Daily selectors never promote (`disabled_market` in their rejection counts); the
prediction itself is still made and stays in All Predictions. Empty the list to allow promotion. SportsGameOdds odds are also restricted to `SGO_ALLOWED_STATS`
(default `points,cornerKicks,yellowCards,weightedCards,bothTeamsScored`) and
`SGO_ALLOWED_PERIODS` (default `game,reg,1h,2h`); anything else is rejected at parse
time (`UNSUPPORTED_STAT` / `UNSUPPORTED_PERIOD`).

Periods supported where a provider offers them: full match, regulation, 1st/2nd
half, quarters and sets/innings for the sports that use them.

**What is intentionally excluded:** SharpAPI player props are dropped — its free
tier gives no game-state/result feed, so those bets could never be settled
honestly. Any market a provider can price but not later settle is never made a
candidate.

---

## How the model works

For each event the bot builds an `EventModel` that blends several independent
signals in logit space, then shrinks the result toward the market and applies a
learned calibration:

1. **De-vigged market probability.** Bookmaker margin is removed with the
   average of a multiplicative and a power de-vig (the power method is fairer to
   longshots). This is the market's honest opinion and the anchor for everything
   else.
2. **Score-grid model** (football, baseball). A Dixon-Coles
   adjusted Poisson grid over final scores. Its expected goals are solved so the
   grid *reproduces* the de-vigged 1X2 and the main Over/Under line. One grid
   prices **every** score-settled market consistently (1X2, DNB, DC, any total or
   Asian line, team totals, BTTS, handicaps, correct score, odd/even, HT/FT,
   result-combos, clean sheet, win-to-nil, winning margin, half markets).
3. **Margin/total model** (basketball, American football). A Gaussian on the
   supremacy and total implied by the spread and total lines and their prices.
4. **Elo** per sport, learned from the results the bot settles over time. Its
   weight grows as it accumulates games, so it contributes little on day one and
   more later.

These are blended by weight, **shrunk toward the market** (default 80%, so the
model never wanders far from the books without reason), and passed through a
**Platt calibration** that refits itself once ≥150 picks have settled. Each
selection ends up with a market probability, a model probability, an agreement
score across signals, and an expected value / edge.

The **collection selectors** (Top and Daily, reading stored predictions) then apply your rules:

- **Top picks** are main markets only (result, DNB, double chance, the main total / handicap), with high model
  probability, at least `TOP_MIN_BOOKS` (3) *distinct* bookmakers, and — where a sharp book priced the line — a price
  no worse than `MIN_EDGE_VS_SHARP` against the sharp fair probability. The 1–3 leg combination with the best
  evidence-adjusted joint confidence inside `TOP_COMBINED_ODDS` (~2.00) – `TOP_ODDS_MAX` is chosen.
- **Daily picks** are sized on their own: the Daily ticket alone aims for `DAILY_COMBINED_MIN_ODDS` (10) and never
  exceeds `DAILY_COMBINED_MAX_ODDS` (12). The builder takes the best-evidenced legs first and **stops as soon as 10
  is reached**; it never pads: if the qualifying legs only make 6.4, 6.4 is posted. A leg needs
  `DAILY_MIN_PROB` (0.55, the single probability floor), `DAILY_MIN_BOOKS` distinct books (2 for main markets, 3 for
  secondary, 4 for restricted ones), books that agree (`MAX_BOOK_DISPERSION`), a lower confidence bound above
  `DAILY_MIN_LCB`, and no clearly negative edge against a sharp reference. `DAILY_PICKS_TARGET` is just a safety cap.
- **Evidence-based ranking** (`predbot/selection/evidence.py`): candidates are ranked by the *lower confidence bound*
  of their probability (probability minus its uncertainty: market-class prior, book disagreement, single-book,
  no-sharp-reference) plus the edge over a sharp reference when one exists — never by raw probability. Market
  quality is graded main / secondary / restricted (first half, corners, far alternate lines, exotics). The ranking score is **not** a probability and is never shown as one.
- **Sharp reference** (`SHARP_BOOKMAKERS`, default Pinnacle / Betfair Exchange / Matchbook): when one of these quotes
  every side of a market, its Shin-de-vigged price is the fair probability; the card shows the *other* books' median
  price, and edge = fair probability × the best of those books' prices − 1 (`EDGE_PRICE`; a median book always carries
  its margin, so it would make every line look negative). Nothing is assumed present: with no sharp
  quote there is no reference, no edge, and a larger uncertainty. `python cli.py accuracy` reports per-leg hit rate,
  Brier vs the market, and results by sport / market / confidence / book count, separately from ticket results.
- **Daily portfolio rules** live only in the Daily selector (`predbot/selection/daily.py`): one leg per match,
  caps on the share of any one market, stat, sport and league, and the combined-odds band. None of them is applied
  while predictions are generated.
- **International football** (UEFA / CONCACAF Nations League, friendlies) is fetched by default and topped up into
  `SHARPAPI_LEAGUES` / `ODDS_API_LEAGUE_ALLOWLIST` even when the environment pins its own lists
  (`INTERNATIONAL_FOOTBALL=false` opts out). ESPN settles them on the full-time score.
- **SportsGameOdds quota**: the bot reads SGO's own `/account/usage` and never spends the last entities of the month
  (they are kept for settlement). When the cap is gone it skips SGO immediately instead of retrying `429`s for minutes;
  settlement falls back to the free results sources.
- **Stale environment values**: a dashboard still pinning a pre-rework default (`DAILY_COMBINED_MAX_ODDS=20`,
  `DAILY_PICKS_TARGET=20`, `DAILY_MIN_PROB=0.30`) is ignored with a warning;
  `HONOR_PINNED_VALUES=1` keeps an old value on purpose. Deliberate values (e.g. `TOP_MIN_BOOKS=2`) are never touched.
Operator view: `/lastrun` shows the generation funnel (fixtures discovered → normalized → analysed → predictions
→ Top/Daily candidates and selected, each drop with its reason), runtime revision, per-provider API/HTTP usage and
provider cache reuse/refresh reasons. Subscriber views never call a provider.

## Architecture

```
Provider discovery -> provider adapters -> canonical normalization -> fixture/event resolution
   -> data enrichment -> prediction engine -> prediction store (ALL PREDICTIONS)
        -> Top Picks | Daily Picks | Stats  -> Telegram
```

- **Canonical identity** (`predbot/storage/identity.py`): competitions, participants and events get stable canonical
  IDs persisted in `canonical_participants`, `canonical_events` and `provider_identity`. Provider IDs are used when a
  provider supplies them; aliases/fuzzy matching run once at first encounter and the mapping is stored, so a
  renamed team does not silently drop a competition.
- **Prediction engine** (`predbot/model/engine.py`): knows nothing about tickets, odds targets or Telegram. For each
  resolved event with enough data it prices the supported markets and keeps ONE eligible representative per market
  family (`predbot/selection/representatives.py`: the modal 1X2 outcome, the double chance covering the two most likely
  outcomes, the favourite for DNB / Moneyline, the likelier BTTS side above 50%, and the likelier side of the MAIN line
  of Over/Under and Handicap; a handicap is kept only when |line| >= 1 and it can win alongside the fixture's other
  predictions). A family with no eligible representative is omitted for that fixture, with a recorded reason. Team
  totals, period markets, corners/cards and props are outside this pool. Bookmaker value is stored as metadata, never
  as a gate on existence.
- **Prediction store**: one row per prediction in `picks`, unique per (day, canonical event, market family); `prediction_memberships` says which collections
  (`all`, `top`, `daily`) it belongs to. It is settled once, whatever it belongs to, and is never regenerated.
- **Selectors** (`predbot/selection/top.py`, `daily.py`, orchestrated by `picker.package_predictions`) read stored
  rows only. Top returns 0-3 legs and never pads to reach its target; Daily owns its portfolio rules. Top and Daily each
  promote at most ONE prediction per fixture.
- **Telegram**: `/all` (sports with predictions, then that sport's fixtures - a fixture's predictions together, ~5 fixtures per page), `/top`, `/daily`, `/stats` (compact Today / 7 days / All time,
  buttons per collection, `View Results` pages). All of them read storage; pagination makes no provider call.
- **Fresh generation**: `python cli.py reset-day [YYYY-MM-DD] [--yes]` invalidates only that day's shared snapshot
  and its unpublished, not-yet-started pending predictions (dry run without `--yes`); settled rows, started
  fixtures, history, subscribers, provider caches and quota counters are kept.

### Independent football model (history -> ratings -> probabilities)

`p_model` is meant to be OUR estimate and `p_market` the bookmaker's; they are stored separately.

- **History**: `match_results` (canonical competition, canonical participants, kickoff, score, source) is filled by
  `python cli.py history-import-csv FILE [competition]` (football-data.co.uk CSVs, no API) or
  `python cli.py history-backfill [--competitions PL,PD] [--seasons 2023,2024] [--yes]` (football-data.org: one request
  per competition-season, dry run until `--yes`, each season fetched once ever, current season at most daily). Results
  settled by the bot also land here, and Elo (keyed by canonical participant id) learns each result exactly once.
- **Model** (`predbot/model/football_strength.py`): time-weighted Poisson attack/defence ratings with a home effect,
  fitted from results only; expected goals feed the existing `ScoreGrid` so 1X2, Double Chance, DNB, Over/Under, BTTS
  and handicaps come from one score distribution. Bookmaker prices play no part.
- **Modes** (`INDEPENDENT_MODEL_MODE`): `shadow` (default) stores `p_independent`, edge vs market, EV and a status next
  to the unchanged `p_model`; `independent` makes `p_model` the independent estimate where one exists; `off`. A line
  without an independent estimate keeps its market-derived value labelled `market_blend`, with the reason
  (`unavailable:no_history | unknown_team | insufficient_history | unsupported_market | no_independent_model`).
- **Backtest**: `python tools/backtest_football.py --db data/predbot.db [--select]` (walk-forward, leak-free; blend
  weights and hyper-parameters are chosen on a validation window, scores come from the later test window).
  `--synthetic` checks the machinery only.

---

### Market comparison research (evaluation only)

`predbot/history/odds_link.py` links free football-data.co.uk CSV odds (Pinnacle, Bet365, market average; pre-match and
closing; 1X2 and Over/Under 2.5) to results **already in the store** - it never inserts a match or creates a participant,
and ambiguous or score-mismatched fixtures are reported rather than guessed. Odds are reference data: no model reads them.

    python tools/audit_history.py                      # duplicates, impossible scores, identity, gaps
    python tools/market_comparison.py --json out.json  # model vs each bookmaker, separately, on the untouched test window

## Setup

### 1. Get API keys

| Provider | Role | Status |
|----------|------|--------|
| **SharpAPI** — https://sharpapi.io/ | fixtures + odds for the explicit `SHARPAPI_LEAGUES` | **required** — the bot exits with code 2 without `SHARPAPI_KEY` |
| **The Odds API** — https://the-odds-api.com/ | odds (Top ticket, European football) + `/scores` results | recommended |
| **SportsGameOdds** — https://sportsgameodds.com/ | NBA/NFL/MLB/MLS/NCAA/UCL odds + results | optional |
| **football-data.org** — https://www.football-data.org/ | free football **results only** (never odds or fixtures) | optional, recommended for settlement |
| **ESPN scoreboard** — site.api.espn.com | free, keyless **results only**: football (incl. Europa/Conference/Süper Lig/Nigeria/MLS), NBA, NCAAB, NFL, NCAAF, MLB, ATP/WTA | on by default (`ESPN_RESULTS=1`) |
| **EuroLeague API** — api-live.euroleague.net | free, keyless Euroleague **results only** | on by default (`EUROLEAGUE_RESULTS=1`) |

A missing optional key only logs a warning at startup; the chain skips that provider.

### 2. Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) → get the token.
2. Add the bot to your channel/group as an admin (or DM it).
3. Get the chat id (e.g. message [@userinfobot](https://t.me/userinfobot), or use
   the numeric channel id like `-1001234567890`).

### 3. Configure

Copy `.env.example` to `.env` and fill it in. The important ones:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SHARPAPI_KEY=...                 # required
ODDS_API_KEY=...                 # recommended
SPORTSGAMEODDS_KEY=...           # optional
FOOTBALL_DATA_KEY=...            # optional (results only)
PROVIDER_ORDER=oddsapi,sharpapi,sportsgameodds
TIMEZONE=Africa/Lagos
POST_TIME=09:00                  # picks are generated
DELIVERY_TIME=09:30              # the card is sent (one instant, in TIMEZONE)
DB_PATH=/data/predbot.db
```

Every knob (pick targets, odds/probability/EV thresholds, budget limits,
schedule) is documented in `.env.example`.

### 4. Run locally

```bash
pip install -r requirements.txt
python cli.py test-post          # verify Telegram works
python cli.py picks --dry-run    # build today's picks without posting
python cli.py picks              # build and post
python cli.py settle             # settle finished games now
python cli.py recap              # post yesterday's recap
python cli.py stats              # print the rolling record
python cli.py budget             # show API usage today / this month
python main.py                   # run the scheduler (what the container runs)
```

`main.py` runs the scheduler loop: recap at `RECAP_TIME`, picks generated at
`POST_TIME`, the card delivered at `DELIVERY_TIME`, a PARTIAL slate retried every
`CACHE_RETRY_SECONDS` until then, and settlement dynamically after the day's last
match ends (+ sport duration + 15 min). With `ENABLE_COMMANDS=true` the bot answers
`/top`, `/daily`, `/stats`, `/yesterday`, `/timezone Area/City` and `/help`; ids in
`TELEGRAM_ADMIN_IDS` also get `/run`, `/settle`, `/recap` and `/lastrun`.
User commands and delivery polling only read the SQLite cache — they never call a
provider. If a generation is running, a read gets "Today's picks are being generated —
try again in a minute." (or the already-published card).

---

## Deploy (justrunmy.app / any Docker host)

1. Build from the repo-root `Dockerfile` (`CMD python main.py`, `DB_PATH=/data/predbot.db`).
   It is a worker — there is no web port. Run exactly ONE instance per Telegram token
   (two pollers cause 409 conflicts).
2. **Mount a persistent volume at `/data`.** The SQLite database holds your record,
   budget counters, Elo ratings and calibration; without a volume every redeploy
   starts empty.
3. Set the variables from `.env.example` in the host's dashboard. **Dashboard values
   override the code defaults**, so delete any variable you no longer want to pin
   (a `PROVIDER_ORDER` that still names a removed provider only produces a startup warning).
4. Redeploy after every push (a restart alone keeps the old image). Startup logs which
   `.env` was loaded and which keys override a default.

---

## Free-tier budgets

The bot tracks usage per provider in the database and refuses to exceed a quota
rather than failing silently:

- **The Odds API** — 500 credits/month; an odds request costs regions × markets
  (`ODDS_API_REGIONS=eu` × 3 markets = 3 credits per league key). Generation is capped
  at `ODDSAPI_MAX_CREDITS_PER_CYCLE=9` (three leagues from `ODDS_API_LEAGUE_ALLOWLIST`),
  never spends below `ODDSAPI_RESERVE_CREDITS=80` of `x-requests-remaining`, and the
  settlement `/scores` call costs 2 credits per league key per run: ≈ 270 + 180 ≈
  450 of 500 a month. `ODDS_API_REGIONS=uk` adds UK books at the same cost.
- **football-data.org** — 10 requests/minute (calls are spaced 6.5 s and cached per
  competition + date for one settlement run).
- **SportsGameOdds** — billed per *object returned* (minimum 1 per response, so `/leagues`,
  empty answers and `/account/usage` all cost), 2,500/month on the Amateur plan, 10 requests/minute,
  rolling 30-day period. The real cap and usage are read from `/account/usage` (rarely); every request
  is attributed (`sgo_usage` table: reason, endpoint, sport, league, objects billed) and gated by
  `predbot/providers/sgo_meter.py` (50/75/90/100 % levels, a 429 cool-down, settlement back-off). Free
  sources settle first; SGO is the fallback. `python cli.py sgo-usage` prints the breakdown.
- **SharpAPI** — the free tier publishes **no monthly quota**, only 12 requests/minute
  (`X-RateLimit-*` headers). Requests are spaced `SHARPAPI_MIN_INTERVAL_SEC` apart and
  budgeted by us: `SHARPAPI_MONTHLY_REQUEST_LIMIT` (our own cap, default 3000) is spread
  over the days left, capped at `SHARPAPI_MAX_REQUESTS_PER_GENERATION` (60), and no feed
  is starved (every league is owed one page). `/odds` is kick-off ordered and a match
  used to cost ~200 rows because alternate lines, corners, cards and player rows ride
  along, so requests now carry `is_main_line=true` (`SHARPAPI_MAIN_LINES_ONLY`) and
  **exact market names** per sport (`SHARPAPI_MARKETS_FOOTBALL`: moneyline, point_spread,
  total_goals, double_chance, draw_no_bet, both_teams_to_score, team_total_goals;
  `SHARPAPI_MARKETS_BASEBALL`: moneyline, run_line, total_runs; other sports use the
  `SHARPAPI_MARKETS` aliases) — about 29 rows per football match instead of 200. Feeds
  are fetched in `SHARPAPI_LEAGUE_PRIORITY` order, a page is never requested past the
  window end, and `SHARPAPI_MAX_PAGES` is a hard per-feed ceiling. `/lastrun` shows each
  feed's requests, rows, matches in window, rows per match and why it stopped. The league
  list is explicit (19 by default; a configured league that nothing can settle is not fetched;
  no sport-level auto-discovery — `SHARPAPI_SPORTS` only logs a warning).
  Set `SHARPAPI_MAIN_LINES_ONLY=0` to get alternate lines back.

Run `python cli.py budget` any time to see where you stand.

---

## Settlement

Every league is one row of `markets/competitions.py` (its SharpAPI slug, Odds API key, SGO
league id, football-data code, ESPN `sport/league`, EuroLeague code). A pick is only made
when `markets/settlement.py::result_paths(event, …)` finds at least one provider that can
settle it, tried in this order: the origin SportsGameOdds id → football-data.org (needs
`FOOTBALL_DATA_KEY`) → **ESPN** → **EuroLeague** → The Odds API `/scores` (2 credits, last
resort, cached per key per day) → a SportsGameOdds team+time lookup.

Each path also declares which **periods and stats** it can grade (football-data and
EuroLeague: full/halves; ESPN: full for football/baseball/tennis, plus halves and
quarters for basketball/NFL; SportsGameOdds: anything). A market no path can grade —
say a 1st-half bet on a `full`-only path — is rejected up front as `PERIOD_UNSETTLEABLE`
(or `STAT_UNSETTLEABLE`) instead of sitting pending for four days and being voided; an
event with no path at all is `NO_RESULTS_PATH`. Leagues with no path are not fetched,
and the startup log prints one table of every configured league and its path(s).

`ResultsResolver` walks the same list, first usable result wins, every provider call is
wrapped so an outage leaves the pick pending. Tennis settles on **sets** (match winner)
and on **games** (totals/handicaps). `settle.run` never raises: a bad pick is counted in
`errors` and the rest are settled; a pick with no result after `MAX_PENDING_DAYS` is
voided. Football markets settle on the 90-minute score.

ESPN needs no key but its edge returns 403 to most user agents, so the provider sends the
default python-requests agent; if ESPN ever blocks the host, its paths fail as
`ProviderError` and settlement falls through to the next path.

---

## Daily cache & zero-API pagination

`/top` and the Top button show the Top ticket (target approximately 2.00); `/daily` and the Daily button show
the Daily ticket. Each is its own card with its own header - `Combined odds` (every pick's price multiplied) and
the number of picks in that ticket, the same on every page - and lists its picks in kickoff order. Commands and
buttons share the same renderer. Kickoff times are shown in the reader's zone, named once in the header
(`times in WAT`); the saved `/timezone` preference (detected from the Telegram locale on `/start`, or set with
`/timezone Area/City`) applies to both commands and buttons, the configured timezone is the fallback, and the
channel post uses the configured zone. The card shows the plain price; where each price came from (median of
N books / a bookmaker) is listed in `/lastrun`.

Buttons never wipe a message. `VIEW_NAVIGATION=edit_pages` (the default): a page turn (◀ ▶) edits the message
it was pressed on, and a view switch (Top / Daily / Stats / Yesterday) sends a NEW message, so the chat keeps every
view it opened. `new` sends a new message for every press; `edit` edits the pressed message every time. A page-turn
button carries its card's date, so an older message keeps turning ITS day's pages.

`/stats` and the recap show settled results only: yesterday's record, hit rate and ROI (only once every pick of
that day is settled - otherwise the record so far plus one completeness line), a per-sport split, the last 7 days
with a per-day strip, and how often the Top ticket landed. `/yesterday` lists every pick issued yesterday with its
result mark, and its totals are the `/stats` numbers.

Predictions use the existing SQLite database (`DB_PATH`) as the only cache.

- `/top`, `/daily`, their buttons, and scheduled publication all use
  `UnifiedFetcher.get_daily_snapshot()` and the same `prediction_cache` row.
- Keys contain the application timezone and calendar date. Subscriber zones affect
  display only. Rows expire at application-zone midnight (Africa/Lagos by default).
- COMPLETE snapshots return without provider calls, including after process restart.
- PARTIAL snapshots remain usable. The `retry_partial` job regenerates a PARTIAL day
  every `CACHE_RETRY_SECONDS` (default 900) **until `DELIVERY_TIME`, and only while
  nothing has been delivered**; successful provider portions in `provider_cache` are
  reused. Regenerating a day nobody was sent replaces that day's pending picks
  (delivered days are never rewritten).
- `force_refresh=True` rebuilds Top/Daily selection from canonical provider data.
  Completed source responses remain protected from unnecessary paid refetches.
- SQLite transactions save both lists/status together; a shared in-process lock
  prevents command and scheduler threads from duplicating generation. Run one bot
  process against this database; this is not a multi-host distributed cache.
- `CACHE_RETENTION_DAYS` defaults to 7. Pruning removes only expired cache history,
  not subscribers, picks, results, settings or API budget records.
- The SQLite database is the only state store: no external cache service, extra package
  or file-cache directory is required. A leftover `CACHE_DIR` setting is unused.
- Startup logs `Prediction cache backend=sqlite` and the resolved SQLite path.
  Docker sets `DB_PATH=/data/predbot.db`; mount persistent storage at `/data`.
  Persistence across container replacement must be verified in JustRunMyApp.
- `python diagnose_prediction_cache.py` is the single cache diagnostic. It opens the
  configured SQLite file read-only (no generation, migration or sports API calls) and
  prints the backend, database path, today's HIT/MISS, COMPLETE/PARTIAL status, sport
  status, football/baseball/Top/Daily counts, generated/updated times, retry-after when
  partial and the cache version. See `SQLITE_CACHE_REPORT.md` for deployment verification.

---

## Tests

```bash
pip install pytest
python -m pytest -q
```

Covers odds/de-vig math, the score-grid model (market recovery + every market's
pricing), settlement grading for every market family (including Asian
quarter-line half-win/half-loss and provider-settled props), the picker rules
(no padding, no overlap, quality bar, diversity caps), and all three provider
parsers against saved sample payloads in `tests/fixtures/`.

---

## Project layout

```
main.py                 scheduler entrypoint (the container worker)
cli.py                  manual commands
predbot/
  config.py             all env-driven settings
  providers/            oddsapi, sharpapi, sportsgameodds; espn, euroleague,
                        football_data (results only), chain, budget
  markets/              canonical market catalogue + settlement grading
  model/                devig, poisson score grid, margin model, elo,
                        calibration (fitted on pre-calibration probabilities), ensemble
  selection/picker.py   top/daily selection rules
  bot/                  telegram client, message formatting, command loop
  jobs/                 daily_picks, settle, recap, stats, scheduler
  storage/db.py         SQLite persistence
tests/                  pytest suite + fixtures
```

### Shared daily delivery time

Picks are generated at `POST_TIME` (default 09:00) and delivered at `DELIVERY_TIME`
(default 09:30), both in `TIMEZONE` (default `Africa/Lagos`): every subscriber gets
the card at the same instant (09:30 WAT = 08:30 UTC), once per person per day. A
subscriber's own zone only changes how kick-off times are displayed. Someone who
subscribes after `DELIVERY_TIME` starts with the next day's card. Legs that have
already started or finished when a card is read are marked. Dashboard variables
override code defaults, so set/delete them there and redeploy.
