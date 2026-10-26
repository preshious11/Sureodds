# Sureodds Telegram Bot (Stage 1 Polling)

Long-polling Telegram bot client running on Node.js, connected to Cloudflare D1 via REST API.

## Architecture

- **Entry point:** `bot.ts` — long-polling loop (`getUpdates`), offset persistence after every update
- **D1 adapter:** `d1-rest-adapter.ts` — REST client implementing the same `prepare().bind().first()/.all()/.run()` surface as the Cloudflare D1 binding
- **Business logic:** imported directly from `../src/` (`workers/src/index.ts`, `db.ts`, etc.) — the same modules the Worker and its own tests use, unmodified. Nothing is copied.
- **Tests:** `tests/` — unit tests (adapter against the Worker's existing test suite) + integration tests (bot against a staging D1 database)

No build step: Node 24 runs `.ts` files directly (native type stripping), the same way `workers/tests/*.test.mjs` already imports `workers/src/*.ts` with no compile stage.

## Setup

```bash
# Install dependencies (dotenv only)
npm install

# Copy .env.example to .env and fill in secrets
cp .env.example .env

# Start the bot
npm start
```

## Environment Variables

See `.env.example`. Required:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `CF_ACCOUNT_ID` — Cloudflare account ID
- `CF_DB_ID` — Cloudflare D1 database ID
- `CF_API_TOKEN` — Scoped to D1 edit on this database only

## Deployment

`/opt/sureodds-bot` is a git working tree, sparse-checked-out to the `workers/` directory only, at a tagged commit — not a copy, the real repo path:

```bash
# One-time, as root:
sudo bash /opt/sureodds-bot/workers/bot/build.sh

# Every deploy, as the sureodds user (no sudo):
sudo -u sureodds bash /opt/sureodds-bot/workers/bot/build.sh
systemctl restart sureodds-telegram-bot
```

## Testing

### Unit tests
```bash
npm test
```

### Staging test (test bot token)
```bash
export TELEGRAM_BOT_TOKEN=<test-bot-token>
npm start
# Send /start to test bot; expect reply within a few seconds
```

## Monitoring

```bash
# Check service status
systemctl status sureodds-telegram-bot

# View logs (live)
journalctl -u sureodds-telegram-bot -f

# Check polling offset
cat /var/lib/sureodds-bot/polling.offset
```

## Rollback

If issues arise:

```bash
systemctl stop sureodds-telegram-bot
curl -X POST https://api.telegram.org/bot{TOKEN}/setWebhook \
  -d url=https://sureodds-worker.example.com/telegram/webhook \
  -d secret_token={WEBHOOK_SECRET}
```

The Worker's webhook code and secret are kept intact (not removed) until Stage 1 has run cleanly for two weeks, so this rollback is always available.

## Stage Progression

- **Stage 1** (current): Bot polling on VM; Cron jobs stay on Worker
- **Stage 2**: Move Cron to systemd timers on VM
- **Stage 3**: Migrate data off D1; retire GitHub Actions

See `docs/STAGE1_IMPLEMENTATION_PLAN.md` for details.
