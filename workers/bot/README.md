# Sureodds Telegram Bot (Stage 1 Polling)

Long-polling Telegram bot client running on Node.js, connected to Cloudflare D1 via REST API.

## Architecture

- **Entry point:** `bot.ts` — polling loop, offset persistence
- **D1 adapter:** `d1-rest-adapter.ts` — REST client for Cloudflare D1
- **Business logic:** `src/` — copied from workers/src/, adapted for Node
- **Tests:** `tests/` — unit tests (adapter) + integration tests (bot)

## Setup

```bash
# Install dependencies
npm install

# Build TypeScript
npm run build

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
- `CF_API_TOKEN` — Scoped to D1 edit on this database

## Deployment

On Oracle VM (as `sureodds` user):

```bash
cd /opt/sureodds-bot
git checkout refs/tags/v1.0-bot-polling
npm install
npm run build
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
# Send /start to test bot; expect reply within 3–5 sec
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

Worker webhook will resume handling updates.

## Stage Progression

- **Stage 1** (current): Bot polling on VM; Cron jobs stay on Worker
- **Stage 2**: Move Cron to systemd timers on VM
- **Stage 3**: Migrate data off D1; retire GitHub Actions

See `docs/STAGE1_IMPLEMENTATION_PLAN.md` for details.
