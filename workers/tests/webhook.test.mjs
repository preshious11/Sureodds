import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  allButtonModeFor,
  buildKeyboard,
  commandToView,
  communityMenuKeyboard,
  encodeCallback,
  isAuthorized,
  menuKeyboard,
  normalizeCommand,
  parseCallback,
  timingSafeEqual,
  topEmptyKeyboard,
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

describe("All Predictions group-to-private keyboards", () => {
  it("allButtonModeFor: private stays callback-based; group uses the deep link when a username is configured", () => {
    assert.equal(allButtonModeFor("private", "SureOddsBot"), "callback");
    assert.deepEqual(allButtonModeFor("group", "SureOddsBot"), { url: "https://t.me/SureOddsBot?start=all" });
    assert.deepEqual(allButtonModeFor("supergroup", "SureOddsBot"), { url: "https://t.me/SureOddsBot?start=all" });
    // Never a dead link: no configured username means the button is omitted, not a broken URL or a callback.
    assert.equal(allButtonModeFor("group", undefined), "omit");
  });
  it("menuKeyboard/buildKeyboard swap the All switch button for a group's deep link (never a callback there)", () => {
    const groupMode = allButtonModeFor("group", "SureOddsBot");
    const menuKb = JSON.stringify(menuKeyboard(groupMode));
    assert.match(menuKb, /https:\/\/t\.me\/SureOddsBot\?start=all/);
    assert.doesNotMatch(menuKb, /"callback_data":"pg\|all\|0"/);
    const built = JSON.stringify(buildKeyboard("top", 0, 1, "2026-10-14", [], groupMode));
    assert.match(built, /https:\/\/t\.me\/SureOddsBot\?start=all/);
    assert.doesNotMatch(built, /"callback_data":"pg\|all\|0"/);
  });
  it("menuKeyboard/buildKeyboard omit the All button entirely in a group with no configured username", () => {
    const omitMode = allButtonModeFor("group", undefined);
    const menuKb = JSON.parse(JSON.stringify(menuKeyboard(omitMode)));
    const flat = JSON.stringify(menuKb);
    assert.doesNotMatch(flat, /All Predictions/);
    assert.doesNotMatch(flat, /"callback_data":"pg\|all\|0"/);
  });
  it("communityMenuKeyboard's All button is always the private deep link, never a callback", () => {
    const kb = JSON.stringify(communityMenuKeyboard("SureOddsBot"));
    assert.match(kb, /https:\/\/t\.me\/SureOddsBot\?start=all/);
    assert.doesNotMatch(kb, /"callback_data":"pg\|all\|0"/);
  });
  it("topEmptyKeyboard (the empty-Top card) also swaps All for the group deep link", () => {
    const groupMode = allButtonModeFor("supergroup", "SureOddsBot");
    const kb = JSON.stringify(topEmptyKeyboard("SureOddsBot", groupMode));
    assert.match(kb, /https:\/\/t\.me\/SureOddsBot\?start=all/);
    assert.doesNotMatch(kb, /"callback_data":"pg\|all\|0"/);
    // Private mode (default) keeps the original callback-based All button.
    const privateKb = JSON.stringify(topEmptyKeyboard("SureOddsBot"));
    assert.match(privateKb, /"callback_data":"pg\|all\|0"/);
  });

  // Bug 10: Top/Daily/Stats/Yesterday used to stay callback buttons in a
  // group even once "all" was fixed this way — a tap posted a brand-new
  // full card into the group every time. They now follow the exact same
  // AllButtonMode-driven rule "all" already used.
  it("menuKeyboard/buildKeyboard also swap Top/Daily/Stats/Yesterday for group deep links, not just All", () => {
    const groupMode = allButtonModeFor("group", "SureOddsBot");
    const menuKb = JSON.stringify(menuKeyboard(groupMode));
    for (const view of ["top", "daily", "stats", "yesterday"]) {
      assert.match(menuKb, new RegExp(`https://t\\.me/SureOddsBot\\?start=${view}`));
      assert.doesNotMatch(menuKb, new RegExp(`"callback_data":"pg\\|${view}\\|0"`));
    }
  });
  it("in a PRIVATE chat, Top/Daily/Stats/Yesterday stay callback buttons, unchanged", () => {
    const menuKb = JSON.stringify(menuKeyboard("callback"));
    for (const view of ["top", "daily", "stats", "yesterday"]) {
      assert.match(menuKb, new RegExp(`"callback_data":"pg\\|${view}\\|0"`));
    }
  });
  it("with no configured username, Top/Daily/Stats/Yesterday fall back to callback rather than a dead link", () => {
    const omitMode = allButtonModeFor("group", undefined);
    const menuKb = JSON.stringify(menuKeyboard(omitMode));
    for (const view of ["top", "daily", "stats", "yesterday"]) {
      assert.match(menuKb, new RegExp(`"callback_data":"pg\\|${view}\\|0"`));
    }
  });
  it("communityMenuKeyboard's Top/Daily/Stats/Yesterday are also always deep links (it is group-only by design)", () => {
    const kb = JSON.stringify(communityMenuKeyboard("SureOddsBot"));
    for (const view of ["top", "daily", "stats", "yesterday"]) {
      assert.match(kb, new RegExp(`https://t\\.me/SureOddsBot\\?start=${view}`));
      assert.doesNotMatch(kb, new RegExp(`"callback_data":"pg\\|${view}\\|0"`));
    }
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
  it("rejects forged/legacy callbacks instead of rendering an unrelated menu", () => {
    // Unknown view: previously fell through to the All Predictions menu.
    assert.equal(parseCallback("pg|evil|0"), null);
    // Python-only views this Worker cannot render.
    assert.equal(parseCallback("pg|results_top|0"), null);
    // Unknown sport suffix.
    assert.equal(parseCallback("pg|all_evil|0"), null);
    // Extra segments: previously silently ignored.
    assert.equal(parseCallback("pg|top|0|20261014|extra"), null);
    // Non-numeric page: Number.parseInt("3abc") === 3, so digits are required.
    assert.equal(parseCallback("pg|top|3abc"), null);
    assert.equal(parseCallback("pg|top|"), null);
    // Malformed day segment.
    assert.equal(parseCallback("pg|top|0|notadate"), null);
  });
  it("still parses every callback shape the Worker itself encodes", () => {
    assert.deepEqual(parseCallback(encodeCallback("top", 0)), { view: "top", page: 0, day: null });
    assert.deepEqual(parseCallback(encodeCallback("all_football", 1, "2026-10-14")), {
      view: "all_football", page: 1, day: "2026-10-14",
    });
    assert.deepEqual(parseCallback(encodeCallback("stats_top", 2)), { view: "stats_top", page: 2, day: null });
  });
  it("splitCommand mirrors production: @BotName stripped, command lowercased, args raw", async () => {
    const { splitCommand } = await import("../src/telegram.ts");
    assert.deepEqual(splitCommand("/top@MyBot extra"), { cmd: "/top", args: ["extra"] });
    assert.deepEqual(splitCommand("/START custom_picks"), { cmd: "/start", args: ["custom_picks"] });
    assert.deepEqual(splitCommand("/stop@MyBot"), { cmd: "/stop", args: [] });
    assert.deepEqual(splitCommand("  "), { cmd: "", args: [] });
  });
});
