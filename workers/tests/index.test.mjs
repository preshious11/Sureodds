import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { welcomeMessage } from "../src/index.ts";

const FIXTURE = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fixtures/golden_welcome.json", import.meta.url)), "utf-8"),
);

describe("shared golden fixture (predictions-bot/tests/test_welcome_golden.py renders the same input)", () => {
  it("welcomeMessage matches the Python production welcome_message byte-for-byte, including HTML escaping", () => {
    for (const { first_name, expected } of FIXTURE.cases) {
      assert.equal(welcomeMessage(first_name), expected, first_name);
    }
  });
});
