# Worker Dependencies Inventory for Stage 1 Bot Polling

## Summary
The Telegram bot path (webhook handler) has **minimal Worker-specific dependencies**. Only 3 items need adaption; everything else is portable.

---

## 1. crypto.randomUUID()

**Location:** `workers/src/index.ts:1176`
```typescript
const token = crypto.randomUUID();  // Generate suggestion token
```

**Worker API:** `SubtleCrypto.randomUUID()` (via globalThis.crypto)  
**Node.js:** Native in Node 15.7+

**Handling on Node:**
```javascript
// No adapter needed; Node.js has crypto.randomUUID() natively
import crypto from 'crypto';
const token = crypto.randomUUID();  // Works identically
```

**Status:** ✅ Works as-is

---

## 2. Request/Response Types

**Location:** `workers/src/index.ts`
- Line 1252: `async fetch(request: Request, env: Env): Promise<Response>`
- Lines 1255–1278: Build Response.json() responses
- Line 1149: `tgApiOnce()` uses `fetch()` to call Telegram API

**Worker API:**
- `Request` object from Cloudflare Worker fetch handler
- `Response.json()` static method

**Node.js:**
- Node 18+ has built-in `fetch()`, but Request/Response are minimal
- Use `node-fetch@2` for full compatibility with Worker code

**Handling on Node:**

```javascript
// bot.js
import fetch from 'node-fetch';

// Polling loop uses standard fetch (same as Worker)
const response = await fetch('https://api.telegram.org/bot.../getUpdates', {
  method: 'POST',
  body: JSON.stringify({ offset, timeout: 30 }),
});
const data = await response.json();
```

**Response.json() static method:**
```typescript
// In index.ts, used in webhook handler
return Response.json({ ok: true }, { status: 200 });

// Node.js adapter (simple wrapper):
// Replace Response with a local ResponseBuilder
class ResponseBuilder {
  static json(body, init) {
    return JSON.stringify(body);  // For logging/testing
    // In production bot: not used (no HTTP response; polling loop)
  }
}
```

**Status:** ✅ node-fetch@2 handles this

---

## 3. ctx.waitUntil()

**Location:** `workers/src/index.ts:1287` (in handleScheduled, the Cron handler)
```typescript
ctx.waitUntil(handleScheduled(env, event.cron, Date.now()));
```

**Context:** This is in the scheduled() entry point, NOT the fetch() webhook handler.

**Reason:** Cron jobs (posting, delivery, recap) stay on the Worker in Stage 1. They use `ctx.waitUntil()` to tell Cloudflare to wait for async work to complete before terminating the Cron handler.

**Handling on Node:**
- Bot polling path: DOES NOT CALL handleScheduled()
- No ctx.waitUntil() needed
- If we ever move Cron to the server (Stage 2), we'll replace it with `await` (Node doesn't need ctx.waitUntil; it naturally waits)

**Status:** ✅ Not needed for Stage 1 bot path

---

## 4. KV, R2, Durable Objects, caches

**Search result:** No usage found in bot path.

**Confirmed by grep:**
```bash
grep -r "KV\|R2\|Durable\|caches\." workers/src/
# No matches in bot path files
```

**Status:** ✅ Not used

---

## 5. D1 Database Binding

**Location:** `workers/src/db.ts` and throughout

**Worker API:**
```typescript
// D1 binding (provided by Cloudflare)
type D1Database = {
  prepare(sql: string): D1PreparedStatement;
}

// Calls look like:
await env.DB.prepare("SELECT * FROM subscribers WHERE chat_id = ?1")
  .bind(chatId)
  .first<T>();
```

**Handling on Node:**
- Replace D1 binding with D1RestAdapter
- D1 REST API: POST to Cloudflare with SQL + params
- Adapter implements same interface: `.prepare().bind().first()/.all()/.run()`
- Bot code uses it identically; no changes to business logic

**Adapter specification:**
```typescript
// d1-rest-adapter.ts
class D1RestAdapter implements D1Database {
  prepare(sql: string): D1PreparedStatement {
    return {
      bind(...params: any[]) {
        return {
          async run() { /* call Cloudflare REST API */ },
          async first<T>(): Promise<T | null> { /* ... */ },
          async all<T>(): Promise<{ results: T[] }> { /* ... */ },
        };
      },
      async run() { /* ... */ },
      async first<T>(): Promise<T | null> { /* ... */ },
      async all<T>(): Promise<{ results: T[] }> { /* ... */ },
    };
  }
}
```

**Status:** ✅ New adapter required (planned)

---

## 6. Telegram API Calls (fetch)

**Location:** `workers/src/index.ts:150–154` (tgApiOnce)
```typescript
const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(payload),
});
```

**Handling on Node:**
- Standard `fetch()` call (works with node-fetch@2 or Node 18+ fetch)
- No changes needed

**Status:** ✅ Works as-is

---

## 7. Global Functions & Objects

| Item | Used? | Handler |
|------|-------|---------|
| `globalThis.crypto.randomUUID()` | ✅ Yes (index.ts:1176) | Native in Node 15.7+ |
| `globalThis.fetch()` | ✅ Yes (index.ts:150) | node-fetch@2 |
| `Request` type | ❌ No (webhook doesn't receive raw Request) | N/A |
| `Response.json()` | ❌ No (polling bot doesn't send HTTP responses) | N/A |
| `KV` | ❌ No | Not used |
| `R2` | ❌ No | Not used |
| `Durable Objects` | ❌ No | Not used |
| `caches` | ❌ No | Not used |
| `ctx.waitUntil()` | ⚠️ Yes, but Cron-only (index.ts:1287) | Stage 2 concern |

---

## 8. Import Differences: Node vs Worker

### Node.js imports:
```typescript
// Node.js (bot.ts)
import fetch from 'node-fetch';
import crypto from 'crypto';
import fs from 'fs';  // for offset persistence
import { config } from 'dotenv';
```

### Worker imports (unchanged):
```typescript
// Workers (src/index.ts)
import { handleUpdate } from './db.ts';  // Standard TS imports
```

**No conflicts; both use ES modules.**

---

## 9. Full Dependency List for bot.js

**npm packages:**
```json
{
  "dependencies": {
    "node-fetch": "^2.7.0",    // fetch() compatibility
    "dotenv": "^16.0.0"        // .env loading
  },
  "devDependencies": {
    "typescript": "^5.0.0",     // Compile Worker code
    "@types/node": "^20.0.0",
    "@types/node-fetch": "^2.6.0"
  }
}
```

**No Worker-specific packages needed.**

---

## 10. Testing Plan

### Unit tests:
1. Run existing Worker test suite against D1RestAdapter
2. Verify same test results on both Worker D1 and REST adapter
3. Test error cases (network timeout, invalid SQL, 401/403)

### Integration tests (new):
1. Create test bot (separate token from @BotFather)
2. Deploy bot.js with test token
3. Send messages: `/start`, `/top`, `/daily`, custom picks
4. Verify responses arrive and D1 is updated
5. Check offset persistence: restart bot, verify no duplicates

### No changes needed to bot business logic; only the database adapter.

---

## Summary Table

| Dependency | Needed? | Handler | Effort |
|------------|---------|---------|--------|
| crypto.randomUUID() | ✅ | Node 15.7+ native | 0h |
| fetch() | ✅ | node-fetch@2 | 0h (add dep) |
| Request/Response | ⚠️ Partial | Handled by fetch polyfill | 1h (remove unused) |
| D1 binding | ✅ | D1RestAdapter | 4h (build + test) |
| KV, R2, Durable Objects | ❌ | Not used | 0h |
| caches | ❌ | Not used | 0h |
| ctx.waitUntil() | ⚠️ Cron only | Stage 2 concern | N/A |

---

**Total effort to adapt Worker code to Node: ~5 hours** (D1 adapter + testing)

