export interface MarketContract {
  sport: string;
  market: string;
  selection: string;
  period: string;
  stat: string;
  entity: string;
  line: number | null;
}

const SOCCER_MARKETS = new Set([
  "result3", "dnb", "dc", "btts", "total", "handicap", "ht_ft", "correct_score",
  "result_total", "result_btts", "odd_even", "clean_sheet", "win_to_nil", "team_to_score",
  "winning_margin",
]);
const CORE_MARKETS = new Set(["result2", "total", "handicap", "odd_even"]);
const PROP_STATS: Record<string, Set<string>> = {
  football: new Set(["points", "goals", "shots", "shots_on_target", "corners", "cards"]),
  basketball: new Set(["points", "rebounds", "assists", "three_pointers", "three_pointers_made",
    "threepointers", "steals", "blocks", "field_goals", "made_field_goals"]),
  american_football: new Set(["points", "passing_yards", "rushing_yards", "receiving_yards",
    "receptions", "touchdowns", "passing_tds", "rushing_tds", "receiving_tds", "field_goals",
    "made_field_goals"]),
  baseball: new Set(["points", "hits", "batting_hits", "total_bases", "totalbases", "runs", "rbi", "rbis",
    "runs_batted_in", "home_runs", "homeruns", "strikeouts", "walks", "doubles", "triples",
    "earned_runs"]),
  tennis: new Set(["points", "games", "sets", "aces", "double_faults", "service_aces"]),
};
const SPORT_MARKETS: Record<string, Set<string>> = {
  football: new Set([...SOCCER_MARKETS, "prop"]),
  american_football: new Set([...CORE_MARKETS, "prop"]),
  basketball: new Set([...CORE_MARKETS, "prop"]),
  baseball: new Set([...CORE_MARKETS, "prop"]),
  tennis: new Set(["result2", "total", "handicap", "prop"]),
};
const SPORT_PERIODS: Record<string, Set<string>> = {
  football: new Set(["full", "reg", "1h", "2h"]),
  american_football: new Set(["full", "reg", "1h", "2h", "1q", "2q", "3q", "4q"]),
  basketball: new Set(["full", "reg", "1h", "2h", "1q", "2q", "3q", "4q"]),
  baseball: new Set(["full", "1i5"]),
  tennis: new Set(["full"]),
};
const OUTCOMES: Record<string, Set<string> | null> = {
  result3: new Set(["home", "draw", "away"]),
  result2: new Set(["home", "away"]),
  dnb: new Set(["home", "away"]),
  dc: new Set(["home_draw", "away_draw", "home_away"]),
  btts: new Set(["yes", "no"]),
  total: new Set(["over", "under"]),
  handicap: new Set(["home", "away"]),
  odd_even: new Set(["odd", "even"]),
  ht_ft: null,
  correct_score: null,
  result_total: null,
  result_btts: null,
  clean_sheet: new Set(["yes", "no"]),
  win_to_nil: new Set(["yes", "no"]),
  team_to_score: new Set(["yes", "no"]),
  winning_margin: null,
  prop: new Set(["over", "under", "yes", "no", "odd", "even"]),
};

export function marketContractReason(value: MarketContract): string | null {
  const { sport, market, selection, period, stat, entity, line } = value;
  if (typeof sport !== "string" || !Object.prototype.hasOwnProperty.call(SPORT_MARKETS, sport)) {
    return "unsupported_sport";
  }
  if (!SPORT_MARKETS[sport].has(market)) return "unsupported_market_for_sport";
  const choices = OUTCOMES[market];
  if (typeof selection !== "string" || !selection) return "missing_outcome";
  if (choices && !choices.has(selection)) return "invalid_outcome";
  if (market === "ht_ft" && !/^(home|draw|away)\/(home|draw|away)$/.test(selection)) return "invalid_outcome";
  if (market === "correct_score" && !/^\d+-\d+$/.test(selection)) return "invalid_outcome";
  if (market === "result_total" && !/^(home|draw|away)_(over|under)$/.test(selection)) return "invalid_outcome";
  if (market === "result_btts" && !/^(home|draw|away)_(yes|no)$/.test(selection)) return "invalid_outcome";
  if (market === "winning_margin" && !/^(home|away)_\d+([+-]\d+)?$/.test(selection)) return "invalid_outcome";
  if (!SPORT_PERIODS[sport].has(period)) return "unsupported_period";
  if (sport === "baseball" && period === "1i5" && market !== "total") return "unsupported_period";
  if (typeof stat !== "string" || !stat) return "missing_stat";
  if (typeof entity !== "string" || !entity) return "invalid_entity";
  if (line != null && (typeof line !== "number" || !Number.isFinite(line))) return "invalid_line";
  if (["result3", "result2", "dnb", "dc", "btts", "odd_even", "ht_ft", "correct_score",
    "result_total", "result_btts", "clean_sheet", "win_to_nil", "team_to_score", "winning_margin"].includes(market)
    && stat !== "points") return "unsupported_stat";
  if (market === "total") {
    const allowed = sport === "football" ? ["points", "goals", "corners", "cards", "shots", "shots_on_target"] :
      sport === "tennis" ? ["games", "sets"] : ["points"];
    if (!allowed.includes(stat)) return "unsupported_stat";
  }
  if (market === "handicap") {
    const allowed = sport === "football" ? ["points", "corners", "cards"] :
      sport === "tennis" ? ["games"] : ["points"];
    if (!allowed.includes(stat)) return "unsupported_stat";
  }
  if (market === "prop") {
    if (!PROP_STATS[sport]?.has(stat.toLowerCase())) return "unsupported_stat";
    if ((selection === "over" || selection === "under") && line == null) return "missing_line";
    if (entity === "all") return "invalid_entity";
  }
  if (["total", "handicap", "result_total"].includes(market) &&
      (line == null || !Number.isFinite(line))) return "missing_line";
  if (market === "total" && !["all", "home", "away"].includes(entity)) return "invalid_entity";
  if (["clean_sheet", "win_to_nil", "team_to_score"].includes(market) &&
      !["home", "away"].includes(entity)) return "invalid_entity";
  if (!["total", "clean_sheet", "win_to_nil", "team_to_score", "prop"].includes(market) && entity !== "all") {
    return "invalid_entity";
  }
  return null;
}
