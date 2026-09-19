# predbot — multi-sport predictions Telegram bot

A Telegram bot for Nigerian SportyBet punters. It pulls odds from The Odds API,
SharpAPI and SportsGameOdds, prices the markets it can settle with its own model,
and posts a daily **Top ticket (~2.00 combined odds) + a Daily ticket**. It settles
results automatically (football-data.org, The Odds API scores, SportsGameOdds) and
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
- **A Top ticket and a Daily ticket**, with hard rules: no padding, no favouring
  one outcome type, one pick per real fixture, and no overlap between the two.
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

`DISABLED_MARKETS` (default `odd_even,prop,winning_margin,ht_ft`) lists market ids
the picker never selects (`DISABLED_MARKET` in the rejection counts); empty it to
allow them. SportsGameOdds odds are also restricted to `SGO_ALLOWED_STATS`
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

The **picker** then applies your rules:

- **Top picks** need high model probability *and* positive EV *and* at least two
  agreeing sources; they are added best-first until the ticket is worth
  `TOP_COMBINED_ODDS` (~2.00), preferring different outcome types.
- **Daily picks** then extend the same ticket until the whole list is worth
  `DAILY_COMBINED_MIN_ODDS`–`DAILY_COMBINED_MAX_ODDS` (10–20). A pick is only
  added when it clears the quality bar, so **it never pads**: if only 11
  qualify, 11 are posted. `DAILY_PICKS_TARGET` is just a safety cap on the
  number of picks in the one list.
- **No overlap:** a top-pick outcome is never repeated in the daily list, and no
  contradictory/equivalent line on the same match is added. The same match *may*
  appear again with a genuinely different outcome.
- **No single-outcome bias:** caps on the share of home/away/over/under picks,
  on any one market type, on short-priced favourites, on exotics, and on player
  props, plus a soft per-sport cap when multiple sports are available.

---

## Setup

### 1. Get API keys

| Provider | Role | Status |
|----------|------|--------|
| **SharpAPI** — https://sharpapi.io/ | fixtures + odds for the explicit `SHARPAPI_LEAGUES` | **required** — the bot exits with code 2 without `SHARPAPI_KEY` |
| **The Odds API** — https://the-odds-api.com/ | odds (Top ticket, European football) + `/scores` results | recommended |
| **SportsGameOdds** — https://sportsgameodds.com/ | NBA/NFL/MLB/MLS/NCAA/UCL odds + results | optional |
| **football-data.org** — https://www.football-data.org/ | free football **results only** (never odds or fixtures) | optional, recommended for settlement |

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
   (an old `PROVIDER_ORDER=api_football,...` only produces a startup warning).
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
- **SportsGameOdds** — billed per *event object* per month
  (`SGO_MONTHLY_OBJECT_LIMIT`). The bot spreads the monthly allowance across the
  days remaining in the month and keeps a reserve for settlement.
- **SharpAPI** — rate-limited to `SHARPAPI_MIN_INTERVAL_SEC` between calls
  (free tier ≈ 12/min). Its `/odds` feed is ordered by kick-off and prices each
  match with dozens of rows, so the fetch is shaped by `SHARPAPI_MARKETS` (default
  `moneyline,spread,total`), an **explicit** `SHARPAPI_LEAGUES` list (19 leagues by
  default; there is no sport-level auto-discovery — a configured `SHARPAPI_SPORTS`
  feed only logs a warning), `SHARPAPI_MAX_PAGES=3` and `SHARPAPI_EVENTS_PER_TARGET`.
  Paging also stops once a page starts after the pick window ends.

Run `python cli.py budget` any time to see where you stand.

---

## Settlement

A pick is only made when `markets/settlement.py::results_path(event, …)` finds a
provider that can settle it, in this order: SportsGameOdds (events it supplied);
The Odds API (its `sport_key` is stored with the pick and `/sports/{key}/scores`
covers it); football-data.org (a football league that maps to a competition code —
needs `FOOTBALL_DATA_KEY`); SportsGameOdds `find_result` for a league it covers.
Otherwise the candidate is rejected as `NO_RESULTS_PATH` (logged once per league).

`ResultsResolver` then resolves each stored pick in this order: the origin provider by
id → alternate ids recorded when providers were merged → football-data.org by
competition + teams + kick-off → SportsGameOdds by teams + time. Every provider call
is wrapped, so an outage leaves the pick pending. `settle.run` never raises: a bad
pick is counted in `errors` and the rest are settled; a pick with no result after
`MAX_PENDING_DAYS` is voided. Football markets settle on the 90-minute score.

---

## Daily cache & zero-API pagination

`/top` and the Top Picks button show the top ticket (target approximately 2.00).
`/daily` and the Daily Picks button show the complete daily ticket: top selections
plus the additional daily selections, which the picker sizes toward 10–20 odds.
Commands and buttons share the same renderer. Each prediction shows its sport
icon, fixture, outcome, confidence, subscriber-local kickoff time and odds.
The footer multiplies all odds in the selected ticket, across every page, and
displays two decimal places. The saved `/timezone` preference (detected from the
Telegram locale on `/start`, or set with `/timezone Area/City`) applies to both
commands and buttons; the configured timezone is the fallback. Each ticket header
shows `odds as of <generation time>` and every pick says where its price comes from
(`Odds: 2.10 (median of 6 books)` / `Odds: 2.35 (Bet365)`).

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
  providers/            oddsapi, sharpapi, sportsgameodds, football_data
                        (results only), chain, budget
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
