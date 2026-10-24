import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { marketContractReason } from "../src/market_contract.ts";

function contract(overrides = {}) {
  return {
    sport: "football",
    market: "result3",
    selection: "home",
    period: "full",
    stat: "points",
    entity: "all",
    line: null,
    ...overrides,
  };
}

describe("sport-aware market contract", () => {
  it("rejects double chance outside soccer", () => {
    for (const sport of ["american_football", "baseball", "basketball"]) {
      assert.equal(
        marketContractReason(contract({ sport, market: "dc", selection: "home_draw" })),
        "unsupported_market_for_sport",
      );
    }
  });

  it("accepts verified baseball first-five totals but rejects other first-five families", () => {
    assert.equal(
      marketContractReason(contract({
        sport: "baseball", market: "total", selection: "over", period: "1i5", line: 4.5,
      })),
      null,
    );
    assert.equal(
      marketContractReason(contract({
        sport: "baseball", market: "result2", selection: "home", period: "1i5",
      })),
      "unsupported_period",
    );
  });

  it("accepts explicit tennis match sets totals and rejects ambiguous point totals", () => {
    assert.equal(
      marketContractReason(contract({
        sport: "tennis", market: "total", selection: "under", stat: "sets", line: 2.5,
      })),
      null,
    );
    assert.equal(
      marketContractReason(contract({
        sport: "tennis", market: "total", selection: "over", stat: "points", line: 20.5,
      })),
      "unsupported_stat",
    );
  });

  it("accepts a fully specified player prop but rejects an unscoped prop", () => {
    assert.equal(
      marketContractReason(contract({
        sport: "baseball", market: "prop", selection: "over", stat: "hits",
        entity: "Alec Bohm", line: 1.5,
      })),
      null,
    );
    assert.equal(
      marketContractReason(contract({
        sport: "baseball", market: "prop", selection: "over", stat: "hits", line: 1.5,
      })),
      "invalid_entity",
    );
  });

  it("accepts supported basketball and baseball moneylines and soccer double chance", () => {
    assert.equal(
      marketContractReason(contract({ sport: "basketball", market: "result2", selection: "home" })),
      null,
    );
    assert.equal(
      marketContractReason(contract({ sport: "baseball", market: "result2", selection: "away" })),
      null,
    );
    assert.equal(
      marketContractReason(contract({ market: "dc", selection: "home_draw" })),
      null,
    );
  });

  it("rejects malformed metadata and invalid lines instead of coercing them", () => {
    assert.equal(marketContractReason(contract({ sport: "toString" })), "unsupported_sport");
    assert.equal(marketContractReason(contract({ entity: "" })), "invalid_entity");
    assert.equal(marketContractReason(contract({ stat: "" })), "missing_stat");
    assert.equal(marketContractReason(contract({ line: "2.5" })), "invalid_line");
  });
});
