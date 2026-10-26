// Unit tests for D1RestAdapter's own HTTP contract: request shape, response
// parsing, error mapping and retry behaviour - against a mocked global
// fetch, not a real Cloudflare account. See
// d1-rest-adapter-worker-compat.test.mjs for tests against real db.ts
// functions and a real (in-memory) sqlite backing store.
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { D1RestAdapter, D1RestError } from "../d1-rest-adapter.ts";

const ACCOUNT_ID = "test-account";
const DB_ID = "test-db-id";
const TOKEN = "test-token";

let originalFetch;
let calls;
let responder;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  calls = [];
  responder = () => jsonResponse(200, { success: true, result: [{ success: true, results: [], meta: { changes: 0 } }] });
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init, body: init?.body ? JSON.parse(init.body) : null });
    return responder();
  };
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("D1RestAdapter construction", () => {
  it("requires accountId, databaseId and apiToken", () => {
    assert.throws(() => new D1RestAdapter("", DB_ID, TOKEN));
    assert.throws(() => new D1RestAdapter(ACCOUNT_ID, "", TOKEN));
    assert.throws(() => new D1RestAdapter(ACCOUNT_ID, DB_ID, ""));
  });

  it("prepare() is synchronous and does not touch the network", () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const stmt = db.prepare("SELECT 1");
    assert.equal(calls.length, 0);
    assert.equal(typeof stmt.bind, "function");
    assert.equal(typeof stmt.first, "function");
    assert.equal(typeof stmt.all, "function");
    assert.equal(typeof stmt.run, "function");
  });
});

describe("D1RestAdapter request shape", () => {
  it("posts sql and params to the per-database query endpoint", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await db.prepare("SELECT * FROM subscribers WHERE chat_id = ?1").bind("123").run();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/d1/database/${DB_ID}/query`);
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers.Authorization, `Bearer ${TOKEN}`);
    assert.deepEqual(calls[0].body, { sql: "SELECT * FROM subscribers WHERE chat_id = ?1", params: ["123"] });
  });

  it("bind() is optional - run()/first()/all() work with zero params", async () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await db.prepare("SELECT 1").run();
    assert.deepEqual(calls[0].body.params, []);
  });
});

describe("D1RestAdapter response parsing", () => {
  it("first() returns the single row", async () => {
    responder = () => jsonResponse(200, { success: true, result: [{ success: true, results: [{ chat_id: "123", active: 1 }] }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const row = await db.prepare("SELECT * FROM subscribers").first();
    assert.deepEqual(row, { chat_id: "123", active: 1 });
  });

  it("first() returns null when there are no rows", async () => {
    responder = () => jsonResponse(200, { success: true, result: [{ success: true, results: [] }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const row = await db.prepare("SELECT * FROM subscribers").first();
    assert.equal(row, null);
  });

  it("all() returns { results: [...] }, matching the D1 binding's own shape", async () => {
    responder = () => jsonResponse(200, { success: true, result: [{ success: true, results: [{ a: 1 }, { a: 2 }] }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const page = await db.prepare("SELECT * FROM t").all();
    assert.deepEqual(page, { results: [{ a: 1 }, { a: 2 }] });
  });

  it("run() surfaces meta.changes - load-bearing for tryClaimUpdate's dedup logic", async () => {
    responder = () => jsonResponse(200, { success: true, result: [{ success: true, results: [], meta: { changes: 1, last_row_id: 7 } }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const result = await db.prepare("INSERT OR IGNORE INTO t VALUES (1)").run();
    assert.equal(result.meta.changes, 1);
    assert.equal(result.meta.last_row_id, 7);
  });

  it("run() defaults meta to {} when Cloudflare omits it, so `.meta.changes ?? 0` never throws", async () => {
    responder = () => jsonResponse(200, { success: true, result: [{ success: true, results: [] }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const result = await db.prepare("UPDATE t SET x = 1").run();
    assert.deepEqual(result.meta, {});
    assert.equal(result.meta.changes ?? 0, 0);
  });
});

describe("D1RestAdapter error handling", () => {
  it("maps HTTP 401 to a non-retryable D1RestError", async () => {
    responder = () => jsonResponse(401, { success: false, errors: [{ message: "invalid token" }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => db.prepare("SELECT 1").run(), (err) => {
      assert.ok(err instanceof D1RestError);
      assert.equal(err.retryable, false);
      assert.match(err.message, /unauthorized/i);
      return true;
    });
    assert.equal(calls.length, 1, "non-retryable error must not retry");
  });

  it("maps HTTP 403 to a non-retryable D1RestError", async () => {
    responder = () => jsonResponse(403, { success: false });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => db.prepare("SELECT 1").run(), /forbidden/i);
    assert.equal(calls.length, 1);
  });

  it("retries on HTTP 500, then succeeds", async () => {
    let attempt = 0;
    responder = () => {
      attempt += 1;
      if (attempt < 3) return jsonResponse(500, { success: false });
      return jsonResponse(200, { success: true, result: [{ success: true, results: [], meta: { changes: 1 } }] });
    };
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    const result = await db.prepare("UPDATE t SET x = 1").run();
    assert.equal(result.meta.changes, 1);
    assert.equal(calls.length, 3);
  });

  it("gives up after the retry budget on a persistent 500", async () => {
    responder = () => jsonResponse(500, { success: false });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => db.prepare("SELECT 1").run());
    assert.equal(calls.length, 3, "should attempt exactly MAX_RETRIES times");
  });

  it("surfaces a D1 query error (success:false in the body) without retrying", async () => {
    responder = () => jsonResponse(200, { success: false, errors: [{ message: "D1_ERROR: near \"FORM\": syntax error" }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => db.prepare("SELECT * FORM t").run(), /syntax error/);
    assert.equal(calls.length, 1);
  });

  it("passes the raw D1 error message through verbatim, so throwIfQuota's regex can still match it", async () => {
    responder = () =>
      jsonResponse(200, { success: false, errors: [{ message: "D1_ERROR: too many rows written: row write limit exceeded" }] });
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => db.prepare("INSERT INTO t VALUES (1)").run(), (err) => {
      assert.match(err.message, /row write limit/i);
      return true;
    });
  });

  it("maps a network failure to a retryable error", async () => {
    globalThis.fetch = async () => {
      throw new TypeError("fetch failed");
    };
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    await assert.rejects(() => db.prepare("SELECT 1").run(), /network error/i);
  });
});

describe("D1RestAdapter unsupported operations", () => {
  it("batch() throws rather than silently misbehaving", () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    assert.throws(() => db.batch(), /not implemented/);
  });

  it("raw() throws rather than silently misbehaving", () => {
    const db = new D1RestAdapter(ACCOUNT_ID, DB_ID, TOKEN);
    assert.throws(() => db.raw(), /not implemented/);
  });
});
