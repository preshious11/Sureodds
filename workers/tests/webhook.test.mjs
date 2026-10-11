import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  commandToView,
  encodeCallback,
  isAuthorized,
  menuKeyboard,
  normalizeCommand,
  parseCallback,
  timingSafeEqual,
} from "../src/telegram.ts";

describe("webhook auth", () => {
  it("rejects missing/wrong secret", () => {
    const req = new Request("https://x/telegram/webhook", {
      method: "POST",
      headers: { "X-Telegram-Bot-Api-Secret-Token": "wrong" },
    });
    assert.equal(isAuthorized(req, "correct-secret"), false);
    const noHeader = new Request("https://x/telegram/webhook", { method: "POST" });
    assert.equal(isAuthorized(noHeader, "correct-secret"), false);
  });
  it("accepts exact secret, rejects empty env", () => {
    const req = new Request("https://x/telegram/webhook", {
      method: "POST",
      headers: { "X-Telegram-Bot-Api-Secret-Token": "s3cret" },
    });
    assert.equal(isAuthorized(req, "s3cret"), true);
    assert.equal(isAuthorized(req, ""), false);
  });
  it("timingSafeEqual is length-sensitive", () => {
    assert.equal(timingSafeEqual("abc", "abc"), true);
    assert.equal(timingSafeEqual("abc", "abd"), false);
    assert.equal(timingSafeEqual("abc", "abcd"), false);
  });
});

describe("commands (mirror of callbacks.py COMMANDS)", () => {
  it("maps all known commands incl. group @suffix and alias", () => {
    assert.equal(normalizeCommand("/top"), "/top");
    assert.equal(normalizeCommand("/top@MyBot"), "/top");
    assert.equal(normalizeCommand("/TODAY extra"), "/today");
    assert.equal(commandToView("/today"), "daily");
    assert.equal(commandToView("/yesterday"), "yesterday");
  });
  it("rejects unknown commands", () => {
    assert.equal(normalizeCommand("/run"), null);
    assert.equal(normalizeCommand("hello"), null);
  });
  it("menu has 4 group-safe buttons, no Open-Bot URL", () => {
    const kb = JSON.stringify(menuKeyboard());
    assert.match(kb, /Top Picks/);
    assert.match(kb, /Daily Picks/);
    assert.doesNotMatch(kb, /t\.me\//);
  });
});

describe("callback codec (mirror of pagination.py pg|view|page|day)", () => {
  it("round-trips view switch and day-pinned page turns", () => {
    assert.equal(encodeCallback("daily", 0), "pg|daily|0");
    const p = parseCallback(encodeCallback("top", 2, "2026-10-11"));
    assert.deepEqual(p, { view: "top", page: 2, day: "2026-10-11" });
  });
  it("rejects noop and malformed payloads", () => {
    assert.equal(parseCallback("noop"), null);
    assert.equal(parseCallback("pg|daily"), null);
    assert.equal(parseCallback("pg|daily|-1"), null);
  });
});
