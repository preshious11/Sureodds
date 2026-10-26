# Stage 1 Build Checklist

**Branch:** `feature/stage1-telegram-polling`  
**Test bot token:** Separate from live (request from @BotFather)  
**Live cutover:** After 1-hour staging test passes and user approval

---

## Phase 1: Setup & Scaffolding

- [ ] Create branch: `git checkout -b feature/stage1-telegram-polling`
- [ ] Create directory: `/workers/bot/`
  ```
  workers/bot/
    ├─ package.json
    ├─ tsconfig.json
    ├─ bot.ts         (main polling loop entry point)
    ├─ d1-rest-adapter.ts
    ├─ src/           (symlink or copy of workers/src/??)
    │   └─ (index.ts, db.ts, custom_picks.ts, telegram.ts, views.ts, format.ts, pro_picks.ts, etc.)
    └─ tests/
        ├─ d1-rest-adapter.test.ts
        └─ polling.integration.test.ts
  ```
- [ ] `package.json` with deps:
  ```json
  {
    "name": "sureodds-telegram-bot",
    "version": "1.0.0",
    "type": "module",
    "scripts": {
      "build": "tsc",
      "start": "node dist/bot.js",
      "test": "node --test tests/**/*.test.ts"
    },
    "dependencies": {
      "node-fetch": "^2.7.0",
      "dotenv": "^16.0.0"
    },
    "devDependencies": {
      "typescript": "^5.2.0",
      "@types/node": "^20.0.0",
      "@types/node-fetch": "^2.6.0"
    }
  }
  ```
- [ ] `tsconfig.json` targeting Node 18
- [ ] `.env.example`:
  ```
  TELEGRAM_BOT_TOKEN=123:abc...
  CF_ACCOUNT_ID=...
  CF_DB_ID=db428f31-...
  CF_API_TOKEN=...
  TELEGRAM_ADMIN_IDS=8606823551
  TELEGRAM_BOT_USERNAME=BETTSUREbot
  TELEGRAM_CHAT_ID=
  TIMEZONE=Africa/Lagos
  NODE_ENV=production
  ```

---

## Phase 2: D1 REST Adapter

**File:** `workers/bot/d1-rest-adapter.ts`

### Implementation checklist:
- [ ] Class D1RestAdapter
  - [ ] Constructor: `(accountId: string, dbId: string, apiToken: string)`
  - [ ] Property: `baseUrl` (computed from accountId/dbId)
  - [ ] Property: `token`

- [ ] Method: `prepare(sql: string)`
  - [ ] Synchronous (returns object, not Promise)
  - [ ] Returns object with methods: `bind()`, `run()`, `first()`, `all()`

- [ ] Method: `bind(...params: any[])`
  - [ ] Optional (sql with no params doesn't need bind)
  - [ ] Chainable: returns object with `run()`, `first()`, `all()`

- [ ] Method: `run()` → Promise<D1Result>
  - [ ] POST to Cloudflare REST endpoint
  - [ ] Parse response: `{ success: true/false, result: [...], errors: [...] }`
  - [ ] Return: `{ success: bool, meta?: { duration: number, ... } }`

- [ ] Method: `first<T>()` → Promise<T | null>
  - [ ] Call REST API with sql + params
  - [ ] Return: `result[0] || null`

- [ ] Method: `all<T>()` → Promise<{ results: T[] }>
  - [ ] Call REST API with sql + params
  - [ ] Return: `{ results: result }`

- [ ] Error handling:
  - [ ] HTTP 401: "Unauthorized (invalid API token)"
  - [ ] HTTP 403: "Forbidden (insufficient scopes)"
  - [ ] HTTP 500: "Cloudflare API error"
  - [ ] D1 query errors (e.g., syntax): throw with error detail
  - [ ] Network timeout: retry up to 3 times with exponential backoff

- [ ] Logging:
  - [ ] Log failed queries to stderr with sql + params
  - [ ] Do NOT log auth tokens or full payloads

### Type definitions:
```typescript
interface D1Result {
  success: boolean;
  meta?: { duration: number };
  errors?: Array<{ message: string; code: string }>;
}

interface D1Database {
  prepare(sql: string): D1PreparedStatement;
}

interface D1PreparedStatement {
  bind(...params: any[]): D1BindingStatement;
  run(): Promise<D1Result>;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<{ results: T[] }>;
}

interface D1BindingStatement {
  run(): Promise<D1Result>;
  first<T>(): Promise<T | null>;
  all<T>(): Promise<{ results: T[] }>;
}
```

---

## Phase 3: Polling Entry Point

**File:** `workers/bot/bot.ts`

### Implementation checklist:
- [ ] Imports:
  ```typescript
  import fetch from 'node-fetch';
  import { config } from 'dotenv';
  import { D1RestAdapter } from './d1-rest-adapter';
  import { handleUpdate } from './src/index';
  import fs from 'fs';
  ```

- [ ] Load environment:
  - [ ] `config()` from dotenv
  - [ ] Read: TELEGRAM_BOT_TOKEN, CF_ACCOUNT_ID, CF_DB_ID, CF_API_TOKEN
  - [ ] Read: TIMEZONE, TELEGRAM_ADMIN_IDS, TELEGRAM_BOT_USERNAME, TELEGRAM_CHAT_ID
  - [ ] Exit with error if required vars missing

- [ ] Build Env object (mocks Worker Env):
  ```typescript
  const env = {
    TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN!,
    DB: new D1RestAdapter(...),
    TIMEZONE: process.env.TIMEZONE || 'Africa/Lagos',
    TELEGRAM_ADMIN_IDS: process.env.TELEGRAM_ADMIN_IDS,
    TELEGRAM_BOT_USERNAME: process.env.TELEGRAM_BOT_USERNAME,
    TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
    // Add any other env vars from wrangler.toml that bot uses
  };
  ```

- [ ] Offset persistence:
  - [ ] Function: `loadOffset()` → read from `/var/lib/sureodds-bot/polling.offset`
  - [ ] Function: `saveOffset(offset)` → write to file (atomically; use temp + rename)
  - [ ] Default offset: 0 (start from beginning on first run)

- [ ] Polling loop:
  ```typescript
  async function pollUpdates() {
    while (true) {
      try {
        const response = await fetch(
          `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              offset,
              timeout: 30,
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
            await saveOffset(offset);  // SAVE AFTER EACH UPDATE
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
  ```

- [ ] Startup:
  ```typescript
  async function main() {
    offset = await loadOffset();
    console.log(`Starting polling from offset ${offset}`);
    await pollUpdates();  // runs forever
  }

  main().catch(err => {
    console.error('Fatal polling error:', err);
    process.exit(1);
  });
  ```

- [ ] Error recovery:
  - [ ] Network timeout: retry after 5 sec
  - [ ] Telegram API 429 (rate limit): respect Retry-After header, backoff
  - [ ] Telegram API 5xx: retry after 5 sec

---

## Phase 4: Adapt Worker Code to Node

**Files to modify:** `workers/src/index.ts`, `workers/src/db.ts`

### Changes:
- [ ] `workers/src/index.ts`:
  - [ ] Line 1252–1289: Remove the `fetch()` handler and `scheduled()` handler
  - [ ] Keep only: `handleUpdate()`, `tgApi()`, `tgApiOnce()`, route handlers
  - [ ] Export `handleUpdate`, `tgApi`, etc. for use in bot.ts

- [ ] `workers/src/db.ts`:
  - [ ] Type: Change `D1Database` parameter from Worker D1 binding to D1RestAdapter
  - [ ] No query changes; adapter is interface-compatible

- [ ] `workers/src/telegram.ts`:
  - [ ] No changes (pure business logic)

- [ ] Handle Response.json() if needed:
  - [ ] Currently only in fetch handler (removed)
  - [ ] If used elsewhere: replace with JSON.stringify()

**Verify:** All imports resolve; no Worker-specific APIs remain.

---

## Phase 5: Write Tests

### Unit tests: `workers/bot/tests/d1-rest-adapter.test.ts`

- [ ] Test `prepare().bind().run()` with valid SQL
- [ ] Test `prepare().bind().first()` returns single row or null
- [ ] Test `prepare().bind().all()` returns array
- [ ] Test `prepare().bind().all()` returns `{ results: [...] }`
- [ ] Test error handling:
  - [ ] HTTP 401 → error with "Unauthorized"
  - [ ] D1 query error → error with SQL detail
  - [ ] Network timeout → retry + error
- [ ] Test SQL injection protection (params are not interpolated)
- [ ] Test batching (if batch() implemented)

### Integration tests: `workers/bot/tests/polling.integration.test.ts`

- [ ] Requires: test Telegram bot token (separate from live)
- [ ] Setup: Connect to staging D1 database
- [ ] Test: Send /start message
  - [ ] Expect webhook reply within 3 sec
  - [ ] Verify subscriber row created in D1
- [ ] Test: Custom Picks conversation
  - [ ] Send sport/market names
  - [ ] Verify custom request stored in D1
  - [ ] Verify reply suggestions
- [ ] Test: Offset persistence
  - [ ] Poll N updates
  - [ ] Kill bot (simulate crash)
  - [ ] Restart bot
  - [ ] Verify no duplicate replies sent
- [ ] Run tests on staging D1, not production

---

## Phase 6: Build & Deploy to Oracle VM

### Build:
```bash
cd workers/bot
npm install
npm run build  # Compile TypeScript to dist/
npm test       # Run unit tests
```

### Deploy to Oracle VM:
```bash
# On Oracle VM, as sureodds user:
cd /opt/sureodds-bot

# Check out bot code (from tagged commit)
git clone https://github.com/preshious11/Sureodds . --sparse 2>/dev/null || true
git sparse-checkout set workers/bot
git fetch origin refs/tags/v1.0-bot-polling
git checkout v1.0-bot-polling

# Build
npm install
npm run build

# Test with test bot token
export TELEGRAM_BOT_TOKEN=<test-bot-token>
export CF_ACCOUNT_ID=...
export CF_DB_ID=...
export CF_API_TOKEN=...
node dist/bot.js &  # Start in background

# Send test messages
# Ctrl+C to stop
```

---

## Phase 7: Staging Test (1 hour)

- [ ] Start bot with test bot token: `systemctl start sureodds-telegram-bot`
- [ ] Verify service is running: `systemctl status sureodds-telegram-bot`
- [ ] Send `/start` to test bot
- [ ] Expect reply within 3–5 sec
- [ ] Check logs: `journalctl -u sureodds-telegram-bot -f`
  - [ ] Should show: "polling from offset 0"
  - [ ] Should show: "Update 123 processed successfully"
  - [ ] No errors, timeouts, or parse failures
- [ ] Send custom picks request: "3 basketball picks"
  - [ ] Expect reply with suggestions
  - [ ] Verify custom_requests table has entry in D1
- [ ] Test offset persistence:
  - [ ] Note current offset: `cat /var/lib/sureodds-bot/polling.offset`
  - [ ] Kill service: `systemctl stop sureodds-telegram-bot`
  - [ ] Wait 5 sec
  - [ ] Restart: `systemctl start sureodds-telegram-bot`
  - [ ] Send test message (same message again)
  - [ ] Verify bot processes it (no duplicate)
  - [ ] Check new offset: should be incremented
- [ ] Monitor for 30 min: no errors, crashes, or hangs
- [ ] Stop service: `systemctl stop sureodds-telegram-bot`

### Success criteria:
- ✅ Replies within 3–5 sec
- ✅ D1 writes work (subscribers, custom requests)
- ✅ No errors in logs
- ✅ Offset persistence survives restart
- ✅ No duplicate replies

---

## Phase 8: Git Commit & Push

### Commit guidelines (per user rules):
- [ ] Author: your identity (preshious11)
- [ ] No AI mentions in commit message
- [ ] Scan for secrets before push:
  ```bash
  git diff --cached | grep -i "token\|secret\|api_key" | grep -v "TELEGRAM_" | grep -v "example"
  ```
- [ ] Do NOT commit `.env` (use `.env.example`)
- [ ] Commit message template:
  ```
  Stage 1: Telegram bot polling on Oracle VM
  
  - Build Node.js bot entry point (bot.ts) for long polling via getUpdates()
  - Implement D1 REST adapter to replace Cloudflare D1 binding
  - Adapt Worker code (index.ts, db.ts) to run on Node.js
  - Add systemd service, offset persistence, error recovery
  - Test with test bot token on staging D1 database
  - Keep Worker webhook code intact for rollback (2 weeks)
  
  Effort: 5 days design + coding + testing
  Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>
  ```

### Push:
```bash
git add workers/bot/ .github/ (systemd unit)
git commit -m "..."
git push origin feature/stage1-telegram-polling
```

---

## Phase 9: User Review & Live Cutover Approval

- [ ] Report to user:
  - Staging test passed (1 hour clean run)
  - No errors in logs
  - D1 reads/writes work
  - Offset persistence verified
  - Ready for live cutover
- [ ] User reviews code
- [ ] User approves live cutover

---

## Phase 10: Live Cutover

**Timing:** Coordinate with user; ideally after a regular message (low load)

- [ ] Pre-flight (5 min before):
  - [ ] `systemctl stop sureodds-telegram-bot`
  - [ ] Verify stopped: `systemctl status sureodds-telegram-bot | grep inactive`
  - [ ] Check offset: `cat /var/lib/sureodds-bot/polling.offset`

- [ ] Delete webhook:
  ```bash
  curl -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
  # Expect: { "ok": true, "result": true }
  ```

- [ ] Start polling bot:
  ```bash
  systemctl start sureodds-telegram-bot
  sleep 5
  systemctl status sureodds-telegram-bot
  ```

- [ ] Verify polling:
  ```bash
  journalctl -u sureodds-telegram-bot -n 20
  # Should show: getUpdates waiting, or updates being processed
  ```

- [ ] Send test message: `/start` to live bot
  - [ ] Expect reply within 3–5 sec
  - [ ] Check logs: `journalctl -u sureodds-telegram-bot -f | grep -E "Update|error"`

- [ ] Monitor (15 min):
  - [ ] No errors
  - [ ] Other users' messages processed
  - [ ] Responses timely

- [ ] Notify admin: "Bot is live and polling"

---

## Phase 11: Post-Cutover Monitoring (2 weeks)

- [ ] Monitor daily:
  - [ ] Check bot processes messages
  - [ ] No crash loops (systemd restart count should be 0–1)
  - [ ] D1 queries succeeding
  - [ ] Logs clean

- [ ] Week 1: Keep webhook code intact (Worker still has it)
- [ ] Week 2: If no issues, tag cutover complete
- [ ] After 2 weeks: Option to remove webhook code from Worker (saves a few bytes)

---

## Blockers & Unknowns

- [ ] Will D1 REST adapter handle all queries used by bot? (Verify in Phase 5 tests)
- [ ] Is 1 GB memory + 2 GB swap sufficient? (Monitor during Phase 7 staging)
- [ ] Does Cloudflare API token scope work as expected? (Test in Phase 5)

---

## Rollback Plan (if needed)

If polling bot has critical errors:
1. `systemctl stop sureodds-telegram-bot`
2. Re-set webhook (Worker handles POSTs again):
   ```bash
   curl -X POST \
     "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
     -d "url=https://sureodds-worker.example.com/telegram/webhook" \
     -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
   ```
3. Verify Worker is receiving: `wrangler tail | grep /telegram/webhook`
4. Root cause investigation: check logs, memory, D1 API errors

---

## Estimated Effort

| Phase | Task | Days |
|-------|------|------|
| 1–2 | Setup, D1 adapter | 2 |
| 3–4 | Polling entry point, adapt Worker code | 1.5 |
| 5 | Tests (unit + integration) | 1 |
| 6–7 | Build, staging test, fixes | 1.5 |
| 8–9 | Commit, review, approval | 0.5 |
| 10–11 | Cutover, monitoring | 0.5 |
| **Total** | | **~7 days** |

---

**Status: READY FOR BUILD (pending user approval of plan)**

