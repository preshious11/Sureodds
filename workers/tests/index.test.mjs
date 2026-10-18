import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { welcomeMessage } from "../src/index.ts";

const FIXTURE = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fixtures/golden_welcome.json", import.meta.url)), "utf-8"),
);

describe("legacy Python golden welcome with production access restrictions", () => {
  it("welcomeMessage preserves legacy greeting/escaping while omitting stats help", () => {
    for (const { first_name, expected } of FIXTURE.cases) {
      // Python menus are legacy; production intentionally omits private admin views.
      assert.equal(welcomeMessage(first_name), expected.split('\n').filter(line=>!line.includes('/stats')).join('\n'), first_name);
    }
  });
});
