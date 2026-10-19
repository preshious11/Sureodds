// Parity tests for splitMessage, a port of predbot/bot/telegram.py's
// _split/_split_long (predictions-bot/tests/test_reliability_batch3.py
// test_message_length_is_measured_in_utf16_units is the direct Python
// equivalent of the first two cases below).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { splitMessage } from "../src/format.ts";
import { sendSplit } from "../src/cron.ts";

describe("splitMessage (Telegram's 4096-char limit, counted in UTF-16 units)", () => {
  it("text at or under the limit is returned whole", () => {
    assert.deepEqual(splitMessage("short"), ["short"]);
    assert.deepEqual(splitMessage("x".repeat(4000), 4000), ["x".repeat(4000)]);
  });

  it("splits on blank-line paragraph boundaries, never inside a block", () => {
    // JS string .length already counts UTF-16 units (a surrogate-pair emoji
    // counts as 2), same as Python's utf16_len - no separate helper needed.
    const text = ["😀".repeat(1500), "😀".repeat(1500), "😀".repeat(1500)].join("\n\n");
    assert.ok(text.length > 4000, "needs splitting at all");
    const chunks = splitMessage(text, 4000);
    assert.equal(chunks.length, 3);
    for (const c of chunks) assert.ok(c.length <= 4000);
  });

  it("a single block longer than the limit is split by words/lines, never losing content", () => {
    const oneBlock = "😀".repeat(2500); // one paragraph, no newlines, over the limit on its own
    const chunks = splitMessage(oneBlock, 4000);
    for (const c of chunks) assert.ok(c.length <= 4000);
    assert.equal(chunks.join(""), oneBlock, "reconstructing the chunks must reproduce the original exactly");
  });

  it("never cuts inside an HTML tag", () => {
    const run = "word ".repeat(50); // plain filler so the cut point can fall anywhere
    const text = `${run}<b>${"x".repeat(80)}</b>${run}`;
    const chunks = splitMessage(text, 120);
    for (const c of chunks) {
      // An odd number of "<" vs matching ">" within one chunk would mean a tag was split.
      const opens = (c.match(/</g) ?? []).length;
      const closes = (c.match(/>/g) ?? []).length;
      assert.equal(opens, closes, `chunk has an unmatched tag boundary: ${JSON.stringify(c)}`);
    }
  });

  it("never cuts inside an HTML entity", () => {
    const text = `${"a".repeat(96)}&amp;${"b".repeat(96)}`;
    const chunks = splitMessage(text, 100);
    for (const c of chunks) assert.ok(!/&(?:[a-z]{0,9})$/i.test(c), `chunk ends mid-entity: ${JSON.stringify(c)}`);
  });

  it("keeps a sticky ticket header with the block that follows it, never left alone", () => {
    const header = "🔥 BET OF THE DAY · Sat 1 Jun";
    const filler = "f".repeat(3000); // fits with the header in the same chunk
    const bigBlock = "b".repeat(3000); // forces a new chunk once added
    const text = [header, filler, bigBlock].join("\n\n");
    const chunks = splitMessage(text, 4000);
    assert.equal(chunks.length, 2);
    assert.ok(chunks[0].startsWith(header), "header stays with the content immediately under it");
    assert.notEqual(chunks[0], header, "header must never be left alone in its own chunk");
  });
});

describe("sendSplit (the sendMessage wrapper every group/owner/match-event send uses)", () => {
  function recorder(okForAll = true) {
    const calls = [];
    const fn = async (_env, _method, params) => {
      calls.push(params);
      return okForAll ? { ok: true, messageId: calls.length } : { ok: false };
    };
    return { calls, fn };
  }

  it("a short text sends exactly one message, markup attached, unchanged from before splitting existed", async () => {
    const r = recorder();
    const markup = { inline_keyboard: [[{ text: "x", url: "https://t.me/x" }]] };
    const result = await sendSplit({}, "-100", "short card", markup, r.fn);
    assert.equal(r.calls.length, 1);
    assert.deepEqual(r.calls[0].reply_markup, markup);
    assert.deepEqual(result, { ok: true, messageId: 1 });
  });

  it("a long text sends multiple chunks, markup on the LAST chunk only, messageId is the FIRST chunk's", async () => {
    const r = recorder();
    const markup = { inline_keyboard: [[{ text: "x", url: "https://t.me/x" }]] };
    const longText = ["a".repeat(3000), "b".repeat(3000)].join("\n\n"); // > 4000, two paragraphs
    const result = await sendSplit({}, "-100", longText, markup, r.fn);
    assert.ok(r.calls.length >= 2, "must split into more than one sendMessage call");
    for (let i = 0; i < r.calls.length - 1; i++) {
      assert.equal(r.calls[i].reply_markup, undefined, `chunk ${i} (not last) must carry no markup`);
    }
    assert.deepEqual(r.calls[r.calls.length - 1].reply_markup, markup, "only the last chunk carries the markup");
    assert.equal(result.messageId, 1, "the receipt's message_id is the FIRST chunk's, not the last's");
    // The blank-line separator becomes the gap BETWEEN messages instead of embedded
    // text, which is the point of splitting on it - re-joining with it must still
    // reproduce the original exactly (no real content lost).
    assert.equal(r.calls.map((c) => c.text).join("\n\n"), longText);
  });

  it("a failure on any chunk reports overall failure", async () => {
    const r = recorder(false);
    const longText = ["a".repeat(3000), "b".repeat(3000)].join("\n\n");
    const result = await sendSplit({}, "-100", longText, undefined, r.fn);
    assert.equal(result.ok, false);
  });
});
