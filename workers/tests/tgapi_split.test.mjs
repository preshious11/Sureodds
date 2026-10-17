// tgApi is the single choke-point every index.ts sendMessage/editMessageText
// call goes through, so splitting (B8) is tested there once instead of at
// each of its ~25 call sites.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { tgApi } from "../src/index.ts";

function stubFetch() {
  const calls = [];
  const fn = async (_url, init) => {
    calls.push(JSON.parse(init.body));
    const id = calls.length; // snapshot now - each Response's .json() must report ITS OWN id, not the live count
    return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: id } }) };
  };
  fn.calls = calls;
  return fn;
}

async function withFetch(fetchImpl, run) {
  const real = globalThis.fetch;
  globalThis.fetch = fetchImpl;
  try {
    return await run();
  } finally {
    globalThis.fetch = real;
  }
}

const env = { TELEGRAM_BOT_TOKEN: "t" };

describe("tgApi splits over-long sendMessage/editMessageText calls", () => {
  it("a short sendMessage is a single call, unchanged", async () => {
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () => tgApi(env, "sendMessage", { chat_id: "1", text: "short" }));
    assert.equal(fetchImpl.calls.length, 1);
  });

  it("an over-long sendMessage splits into several calls, markup on the last only", async () => {
    const fetchImpl = stubFetch();
    const markup = { inline_keyboard: [[{ text: "x", url: "https://t.me/x" }]] };
    const longText = ["a".repeat(3000), "b".repeat(3000)].join("\n\n");
    const res = await withFetch(fetchImpl, () =>
      tgApi(env, "sendMessage", { chat_id: "1", text: longText, reply_markup: markup }),
    );
    assert.ok(fetchImpl.calls.length >= 2);
    for (let i = 0; i < fetchImpl.calls.length - 1; i++) {
      assert.equal(fetchImpl.calls[i].reply_markup, undefined);
    }
    assert.deepEqual(fetchImpl.calls.at(-1).reply_markup, markup);
    const body = await res.json();
    assert.equal(body.result.message_id, 1, "the returned Response is the FIRST chunk's");
  });

  it("an over-long editMessageText falls back to split sendMessage calls, dropping message_id", async () => {
    const fetchImpl = stubFetch();
    const longText = ["a".repeat(3000), "b".repeat(3000)].join("\n\n");
    await withFetch(fetchImpl, () =>
      tgApi(env, "editMessageText", { chat_id: "1", message_id: 42, text: longText, parse_mode: "HTML" }),
    );
    assert.ok(fetchImpl.calls.length >= 2, "falls back to one or more sendMessage calls");
    for (const call of fetchImpl.calls) {
      assert.equal(call.message_id, undefined, "message_id must not leak into a fresh send");
    }
  });

  it("a short editMessageText is still a real edit, not redirected", async () => {
    const fetchImpl = stubFetch();
    await withFetch(fetchImpl, () =>
      tgApi(env, "editMessageText", { chat_id: "1", message_id: 42, text: "short" }),
    );
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(fetchImpl.calls[0].message_id, 42);
  });

  it("a failure partway through a split send surfaces as a thrown error", async () => {
    let n = 0;
    const fetchImpl = async () => {
      n += 1;
      if (n === 2) return { ok: false, status: 400, json: async () => ({ description: "boom" }) };
      return { ok: true, status: 200, json: async () => ({ ok: true, result: { message_id: n } }) };
    };
    const longText = ["a".repeat(3000), "b".repeat(3000)].join("\n\n");
    await assert.rejects(
      () => withFetch(fetchImpl, () => tgApi(env, "sendMessage", { chat_id: "1", text: longText })),
      /telegram sendMessage failed/,
    );
  });
});
