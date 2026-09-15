# predbot — multi-sport predictions Telegram bot

A Telegram bot that pulls odds from three free-tier APIs, prices **every market
the APIs expose** with its own model, and posts a daily set of **2 top picks +
up to 20 daily picks**. It settles results automatically and tracks a rolling
record.

It is written in pure Python (only `requests` is required) and is built to run
as a single Railway **worker** service.

> **Honest note first.** No model can guarantee betting results — sports are
> genuinely uncertain and every book builds in a margin. This bot estimates
> probabilities and expected value and is transparent about how; it is a
> research/automation tool, not a money printer. Treat "top picks" as *the
> model's most confident estimates*, not sure things. Bet responsibly, only what
> you can afford to lose, and check the gambling laws where you live.

---

## What it does

- **Fallback provider chain.** It queries providers in order (default
  `api_football → sportsgameodds → sharpapi`) and stops once it has enough
  priced events for the day. If football alone is thin, it reaches into the
  other providers for basketball, tennis, hockey, baseball, etc. Events that
  appear in more than one provider are merged (fuzzy team-name + kickoff match),
  so a pick can be backed by several books.
- **Full market coverage.** Not just win/lose — see the list below.
- **A real pricing model** (not just the odds read back at you) — see "How the
  model works".
- **2 top picks + up to 20 daily picks**, with hard rules: no padding, no
  favouring one outcome type, and no overlap between the two pools.
- **Auto-settlement + stats.** Results are fetched from the APIs; it posts a
  daily recap (each pick ✅/❌, win–loss, best win, 7-day ROI) and a rolling
  7-day record.

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
| Other | Odd/Even (match & team) |
| Props | Player props (Over/Under, Yes/No) **only via SportsGameOdds**, since only it supplies a settleable result value |

Periods supported where a provider offers them: full match, regulation, 1st/2nd
half, quarters, and periods (hockey).

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
2. **Score-grid model** (football, hockey, handball, baseball). A Dixon-Coles
   adjusted Poisson grid over final scores. Its expected goals are solved so the
   grid *reproduces* the de-vigged 1X2 and the main Over/Under line, then blended
   with API-Football's season / last-5 scoring rates when available. One grid
   prices **every** score-settled market consistently (1X2, DNB, DC, any total or
   Asian line, team totals, BTTS, handicaps, correct score, odd/even, HT/FT,
   result-combos, clean sheet, win-to-nil, winning margin, half markets).
3. **Margin/total model** (basketball, American football). A Gaussian on the
   supremacy and total implied by the spread and total lines and their prices.
4. **Elo** per sport, learned from the results the bot settles over time. Its
   weight grows as it accumulates games, so it contributes little on day one and
   more later.
5. **API-Football `/predictions`** percentages, used as an extra 1X2 signal for
   football.

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

### 1. Get API keys (all have free tiers)

- **API-Football** — https://www.api-sports.io/ (direct) or via RapidAPI.
- **SportsGameOdds** — https://sportsgameodds.com/
- **SharpAPI** — https://sharpapi.io/

You only need the ones you want to use; the chain skips any provider without a
key.

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
API_FOOTBALL_KEY=...
SPORTSGAMEODDS_KEY=...
SHARPAPI_KEY=...
PROVIDER_ORDER=api_football,sportsgameodds,sharpapi
POST_TIME=09:00
RECAP_TIME=08:30
TIMEZONE=Africa/Lagos
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
python main.py                   # run the scheduler (what Railway runs)
```

`main.py` runs the scheduler loop: recap at `RECAP_TIME`, picks at `POST_TIME`,
settlement every `SETTLE_INTERVAL_MIN` minutes. Set `ENABLE_COMMANDS=true` and
`TELEGRAM_ADMIN_IDS` to also answer `/stats`, `/today`, `/yesterday` (and admin
`/run`, `/settle`, `/recap`).

---

## Deploy on Railway

1. Push this folder to a GitHub repo and create a Railway project from it (or use
   the Railway CLI).
2. Railway reads the `Procfile` (`worker: python main.py`) and `railway.json`.
   It runs as a **worker** — there is no web port.
3. **Add a Volume** and mount it at `/data`, then set `DB_PATH=/data/predbot.db`.
   This is essential: the SQLite database holds your record, budget counters,
   Elo ratings and calibration. Without a volume the container's disk resets on
   every redeploy and you lose all history.
4. Add every variable from `.env` in the Railway **Variables** tab.
5. Deploy. Watch the logs; run a one-off `python cli.py test-post` from the
   Railway shell to confirm Telegram is wired up.

---

## Free-tier budgets

The bot tracks usage per provider in the database and refuses to exceed a quota
rather than failing silently:

- **API-Football** — requests/day (`APIF_DAILY_LIMIT`, default 100). It reserves
  a slice for end-of-day settlement.
- **SportsGameOdds** — billed per *event object* per month
  (`SGO_MONTHLY_OBJECT_LIMIT`). The bot spreads the monthly allowance across the
  days remaining in the month and keeps a reserve for settlement.
- **SharpAPI** — rate-limited to `SHARPAPI_MIN_INTERVAL_SEC` between calls
  (free tier ≈ 12/min).

Run `python cli.py budget` any time to see where you stand.

---

## Daily cache & zero-API pagination

Interactive Telegram reads never touch the provider APIs:

- The whole day's data (top picks, daily picks, stats) is normalised into one
  `DailySnapshot` and cached **once per UTC day** under
  `bot:predictions:daily:{YYYY-MM-DD}`; the entry expires at UTC midnight.
- `/today` posts page 0 with inline buttons; ◀️ ▶️ and the Top / Daily / Stats
  switch all **edit the existing message from the cached snapshot** — zero
  external API requests per navigation.
- A cache miss takes the per-day mutex (`bot:predictions:lock:{...}`), re-checks
  the cache, then rebuilds from the SQLite picks table (still zero API) and only
  falls back to a full provider fetch when nothing is published yet. Concurrent
  readers wait for the builder's cache entry instead of duplicating API calls.
- The default backend is an in-process TTL dictionary; set `REDIS_URL` (and
  `pip install redis`) to share the cache and lock across replicas.

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
main.py                 scheduler entrypoint (Railway worker)
cli.py                  manual commands
predbot/
  config.py             all env-driven settings
  providers/            sportsgameodds, sharpapi, api_football, chain, budget
  markets/              canonical market catalogue + settlement grading
  model/                devig, poisson score grid, margin model, elo,
                        features, calibration, ensemble
  selection/picker.py   top/daily selection rules
  bot/                  telegram client, message formatting, command loop
  jobs/                 daily_picks, settle, recap, stats, scheduler
  storage/db.py         SQLite persistence
tests/                  pytest suite + fixtures
```
