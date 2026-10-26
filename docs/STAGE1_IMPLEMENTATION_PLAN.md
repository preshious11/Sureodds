# Stage 1 Implementation Plan: Telegram Bot Polling on Oracle VM

## Approved by user with conditions (2026-10-26)

---

## 1. Worker-Only Dependencies Inventory

### Used in bot path (webhook + Custom Picks):
- **crypto.randomUUID()** (line index.ts:1176)
  - Handler: Node.js 15.7+ includes `crypto.randomUUID()` natively
  - No changes needed

- **Request/Response types** (line index.ts:1252, 1255-1278)
  - Handler: Use `node-fetch@2` or Node 18+ built-in fetch
  - Minimal wrapper: Adapt Response.json() calls to JSON string + headers dict
  - Lines: index.ts tgApiOnce (build Request, read Response)

- **ctx.waitUntil()** (line index.ts:1287)
  - Location: handleScheduled() — NOT used by webhook path
  - Status: Cron jobs stay on Worker; bot polling has no background tasks
  - No adapter needed

### NOT used:
- KV, R2, Durable Objects, caches
- No usage found; safe to ignore

---

## 2. D1 REST Adapter Specification

### Operations required (from db.ts audit):
- `prepare(sql: string)` → synchronous, returns object with bind/first/all/run
- `bind(...params: any[])` → chainable, optional
- `first<T>()` → Promise<T | null>
- `all<T>()` → Promise<{results: T[]}>  [Note: .all() returns {results:[]}]
- `run()` → Promise<D1Result>

### NOT required:
- batch() — not used in bot path
- raw() — not used in bot path

### Implementation constraints:
- Synchronous prepare() like the D1 binding (return deferred object, not Promise)
- Parse Cloudflare D1 REST response correctly
  - Success: `{ success: true, result: [] }`
  - Error: `{ success: false, errors: [...] }`
- Test against Worker tests (existing suite)
- Integration tests on staging D1 database

### Unsupported calls:
- None identified. All operations are standard CRUD.

---

## 3. Directory Structure on Oracle VM

```
/opt/sureodds-bot/
  ├─ package.json                    # Node deps: node-fetch@2, dotenv
  ├─ bot.js                          # Entry point: polling loop
  ├─ d1-rest-adapter.js              # D1 REST client
  ├─ build.sh                        # Build script (npm install, git checkout)
  ├─ .env                            # Secrets (TELEGRAM_BOT_TOKEN, CF_API_TOKEN, etc.)
  ├─ .env.example                    # Template
  ├─ src/
  │  ├─ (copied from workers/src)    # index.ts, custom_picks.ts, db.ts, telegram.ts, views.ts, format.ts, etc.
  │  └─ polling-adapter.ts           # Replace D1 binding with REST adapter in Env
  ├─ tsconfig.json                   # TypeScript config for Node
  └─ venv/ or .pnpm/                 # Dependency cache (optional)
```

### Deployment method:
```bash
# On Oracle VM, as sureodds user:
cd /opt/sureodds-bot
git clone https://github.com/preshious11/Sureodds.git . --sparse
git sparse-checkout set workers
git checkout v1.0-bot-polling          # or specific tag
npm install
npm run build                          # Compile TypeScript
systemctl restart sureodds-telegram-bot
```

---

## 4. Polling Loop Architecture

### Entry point (bot.js):
```javascript
import fetch from 'node-fetch';
import { config } from 'dotenv';
import { D1RestAdapter } from './d1-rest-adapter.js';
import { handleUpdate } from './src/index.js';

config();

const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const CF_ACCOUNT_ID = process.env.CF_ACCOUNT_ID;
const CF_DB_ID = process.env.CF_DB_ID;
const CF_API_TOKEN = process.env.CF_API_TOKEN;

const adapter = new D1RestAdapter(CF_ACCOUNT_ID, CF_DB_ID, CF_API_TOKEN);
let offset = 0;
let lastSavedOffset = 0;

const env = {
  TELEGRAM_BOT_TOKEN,
  DB: adapter,
  TIMEZONE: process.env.TIMEZONE || 'Africa/Lagos',
  TELEGRAM_ADMIN_IDS: process.env.TELEGRAM_ADMIN_IDS,
  TELEGRAM_BOT_USERNAME: process.env.TELEGRAM_BOT_USERNAME,
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
  // ... other env vars from wrangler.toml
};

async function pollUpdates() {
  while (true) {
    try {
      const response = await fetch(
        `https://api.telegram.org/bot${TELEGRAM_TOKEN}/getUpdates`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            offset: offset,
            timeout: 30,  // long polling: wait 30 sec for updates
            allowed_updates: ['message', 'callback_query'],
          }),
        }
      );
      const data = await response.json();

      if (!data.ok) {
        console.error('getUpdates failed:', data);
        await sleep(5000);
        continue;
      }

      for (const update of data.result) {
        try {
          await handleUpdate(env, update);
          offset = update.update_id + 1;
          // SAVE OFFSET AFTER EACH UPDATE (not batched)
          await saveOffset(offset);
          lastSavedOffset = offset;
        } catch (err) {
          console.error(`Update ${update.update_id} failed:`, err);
          // Do NOT increment offset; retry next poll
        }
      }
    } catch (err) {
      console.error('Poll cycle failed:', err);
      await sleep(5000);
    }
  }
}

async function saveOffset(offset) {
  // Write to a simple file or D1
  // For now: simple in-memory state persisted to a file
  // At crash: resume from last saved offset (no resend)
  fs.writeFileSync('/var/lib/sureodds-bot/polling.offset', String(offset));
}

async function loadOffset() {
  try {
    return parseInt(fs.readFileSync('/var/lib/sureodds-bot/polling.offset', 'utf8') || '0');
  } catch {
    return 0;
  }
}

async function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

offset = await loadOffset();
pollUpdates().catch(err => {
  console.error('Fatal polling error:', err);
  process.exit(1);
});
```

### Offset persistence:
- **File location:** `/var/lib/sureodds-bot/polling.offset`
- **Timing:** Save after each handleUpdate succeeds
- **Crash scenario:** Resume from last saved offset; no duplicate sends
- **Permission:** `sureodds` user owns file; mode 600

---

## 5. D1 REST Adapter Implementation

File: `d1-rest-adapter.js`

### Response shape:
```
Success: { success: true, result: [{...}, {...}] }
Error:   { success: false, errors: [{message: "...", code: "..."}] }
```

### Interface:
```typescript
class D1RestAdapter {
  constructor(accountId: string, dbId: string, apiToken: string)
  
  prepare(sql: string) {
    // Synchronous; returns object with optional bind()
    return {
      bind: (...params) => ({
        run: () => Promise<D1Result>,
        first: () => Promise<T | null>,
        all: () => Promise<{ results: T[] }>,
      }),
      run: () => Promise<D1Result>,
      first: () => Promise<T | null>,
      all: () => Promise<{ results: T[] }>,
    }
  }

  batch(statements: Array<{ sql, params }>) {
    // Atomic: execute all or none
    // Return: Promise<D1Result[]>
  }
}
```

### Error handling:
- Cloudflare API errors (401, 403, 500): throw with descriptive message
- D1 query errors: throw with SQL error detail
- Network timeouts: retry with exponential backoff (3 attempts)

### Unsupported operations:
- None in the bot path

---

## 6. Testing Strategy

### Unit tests:
- Run existing Worker test suite against D1RestAdapter
- Add integration tests for bot polling (use test bot token)
- Test offset persistence and crash recovery

### Staging test:
- Deploy to Oracle VM with test bot token
- Run for 1 hour: send /start, /top, custom picks, etc.
- Verify D1 writes (queries run successfully)
- Check logs for errors

### Live test preparation:
- Test rollback: start polling, then stop service + re-set webhook
- Verify Worker still receives Telegram POSTs

---

## 7. Secrets & Security

### Environment variables (.env):
```
TELEGRAM_BOT_TOKEN=123:abc...       # Test or live bot token
CF_ACCOUNT_ID=...                    # Cloudflare account ID
CF_DB_ID=<your-d1-database-id>       # D1 database ID
CF_API_TOKEN=...                     # Scoped to D1 edit on this database ONLY
TELEGRAM_ADMIN_IDS=<your-telegram-admin-user-id>
TELEGRAM_BOT_USERNAME=<your-bot-username>
TELEGRAM_CHAT_ID=...                 # (optional)
```

### Token scoping:
- CF_API_TOKEN must be scoped to:
  - Account: this Cloudflare account
  - Resource: D1 database sureodds_production
  - Permissions: Account:D1:Edit only
  - Create via Cloudflare UI: API Tokens > Custom Token > D1 Edit

### File permissions:
- `/opt/sureodds-bot/.env`: mode 600, owned by sureodds
- `/var/lib/sureodds-bot/polling.offset`: mode 600, owned by sureodds

---

## 8. Systemd Service Configuration

File: `/etc/systemd/system/sureodds-telegram-bot.service`

```ini
[Unit]
Description=Sureodds Telegram Bot (Long Polling)
After=network.target

[Service]
Type=simple
User=sureodds
WorkingDirectory=/opt/sureodds-bot
ExecStart=/usr/bin/node bot.js
Restart=always
RestartSec=5
StartLimitInterval=60
StartLimitBurst=3
StandardOutput=journal
StandardError=journal
Environment="NODE_ENV=production"
EnvironmentFile=/opt/sureodds-bot/.env
MemoryMax=512M
MemoryAccounting=yes
# sureodds user gets NO sudo (explicit, per requirement)

[Install]
WantedBy=multi-user.target
```

### Memory limits:
- MemoryMax=512M: process killed if it exceeds 512 MB
- 1 GB VM + 2 GB swap: safe headroom
- systemd restarts automatically on OOM

---

## 9. Cutover Plan (Detailed)

### Day -1: Staging
- [ ] Deploy bot code to staging directory on Oracle VM
- [ ] Create test Telegram bot at @BotFather (separate token)
- [ ] Update .env with test token
- [ ] Start service: `systemctl start sureodds-telegram-bot`
- [ ] Send /start message to test bot; verify reply in logs
- [ ] Monitor for 30 minutes; check for errors
- [ ] Stop service: `systemctl stop sureodds-telegram-bot`
- [ ] Report: "Staging test passed; ready for live cutover"

### Day 0: Live Cutover (~2 min downtime)
1. **Pre-flight (5 min before)**
   - [ ] Verify polling service is stopped: `systemctl status sureodds-telegram-bot | grep inactive`
   - [ ] Save current polling offset: `cat /var/lib/sureodds-bot/polling.offset`

2. **Delete webhook** (Telegram side; immediate)
   ```bash
   curl -X POST \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
   # Expected response: { "ok": true, "result": true }
   ```

3. **Start polling bot**
   ```bash
   systemctl start sureodds-telegram-bot
   sleep 5
   systemctl status sureodds-telegram-bot  # verify active
   ```

4. **Verify Telegram is polling**
   ```bash
   journalctl -u sureodds-telegram-bot -n 20  # check logs
   # Should show: getUpdates timeout waiting for new messages
   ```

5. **Send test message**
   - Send `/start` to live bot
   - Expect reply within 3–5 seconds
   - Check logs: `journalctl -u sureodds-telegram-bot -f | grep update_id`

6. **Monitor** (15 min)
   - Watch logs for errors, duplicate sends, parsing issues
   - Check if other users' messages are processed

7. **Notify admin**
   - "Bot is now polling; webhook is offline"

---

## 10. Rollback (< 1 min)

**If polling bot has errors:**
1. Stop polling bot: `systemctl stop sureodds-telegram-bot`
2. Re-set webhook (Worker handles it):
   ```bash
   curl -X POST \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -d "url=https://sureodds-worker.example.com/telegram/webhook" \
     -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
   ```
3. Verify Worker is receiving: `wrangler tail | grep /telegram/webhook`

**Keep webhook code + secret intact** in Worker for 2 weeks after go-live, then remove.

---

## 11. Success Criteria for Stage 1

- [ ] Polling bot responds to /start within 3–5 sec
- [ ] Custom Picks conversation works (send sport/market names)
- [ ] D1 writes succeed (subscribers, custom requests stored)
- [ ] Offset persistence: crash and resume without duplicate sends
- [ ] No logs: errors, timeouts, parse failures
- [ ] Worker Cron still posts daily picks at 09:00 Lagos time
- [ ] Run cleanly for 2 weeks before removing webhook code

---

## 12. Work Branches & Push Checklist

- Branch: `feature/stage1-telegram-polling`
- Before push:
  - [ ] No AI mentions in commits
  - [ ] Author: your identity only (preshious11)
  - [ ] Scan for secrets (tokens, keys, URLs)
  - [ ] No .env file in git (use .env.example)

---

## Build & Deploy Tasks (following approval)

1. Create D1 REST adapter (d1-rest-adapter.js)
2. Create polling entry point (bot.js)
3. Adapt Worker code to Node (handle Request/Response, crypto)
4. Write unit tests (test adapter against Worker tests)
5. Write integration tests (test bot with staging D1)
6. Create systemd service file
7. Document .env.example
8. Tag commit: v1.0-bot-polling
9. Test staging with test bot token (report results)
10. Cutover to live (deleteWebhook, start service, monitor)

---

**Status: PLAN APPROVED, PENDING IMPLEMENTATION**

