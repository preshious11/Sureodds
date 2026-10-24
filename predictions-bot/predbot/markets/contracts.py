"""Sport-aware market contracts and quarantine for ambiguous legacy mappings."""
import math
import re


_SOCCER_MARKETS = {
    "result3", "dnb", "dc", "btts", "total", "handicap", "ht_ft", "correct_score",
    "result_total", "result_btts", "odd_even", "clean_sheet", "win_to_nil", "team_to_score",
    "winning_margin",
}
_CORE_MARKETS = {"result2", "total", "handicap", "odd_even"}
_PROP_STATS = {
    "football": {"points", "goals", "shots", "shots_on_target", "corners", "cards"},
    "basketball": {"points", "rebounds", "assists", "three_pointers", "three_pointers_made",
                   "threepointers", "steals", "blocks", "field_goals", "made_field_goals"},
    "american_football": {"points", "passing_yards", "rushing_yards", "receiving_yards",
                          "receptions", "touchdowns", "passing_tds", "rushing_tds",
                          "receiving_tds", "field_goals", "made_field_goals"},
    "baseball": {"points", "hits", "batting_hits", "total_bases", "totalbases", "runs", "rbi", "rbis",
                 "runs_batted_in", "home_runs", "homeruns", "strikeouts", "walks",
                 "doubles", "triples", "earned_runs"},
    "tennis": {"points", "games", "sets", "aces", "double_faults", "service_aces"},
}
_SPORT_MARKETS = {
    "football": _SOCCER_MARKETS | {"prop"},
    "american_football": _CORE_MARKETS | {"prop"},
    "basketball": _CORE_MARKETS | {"prop"},
    "baseball": _CORE_MARKETS | {"prop"},
    "tennis": {"result2", "total", "handicap", "prop"},
}
_SPORT_PERIODS = {
    "football": {"full", "reg", "1h", "2h"},
    "american_football": {"full", "reg", "1h", "2h", "1q", "2q", "3q", "4q"},
    "basketball": {"full", "reg", "1h", "2h", "1q", "2q", "3q", "4q"},
    "baseball": {"full", "1i5"},
    "tennis": {"full"},
}
_OUTCOMES = {
    "result3": {"home", "draw", "away"},
    "result2": {"home", "away"},
    "dnb": {"home", "away"},
    "dc": {"home_draw", "away_draw", "home_away"},
    "btts": {"yes", "no"},
    "total": {"over", "under"},
    "handicap": {"home", "away"},
    "odd_even": {"odd", "even"},
    "ht_ft": None,
    "correct_score": None,
    "result_total": None,
    "result_btts": None,
    "clean_sheet": {"yes", "no"},
    "win_to_nil": {"yes", "no"},
    "team_to_score": {"yes", "no"},
    "winning_margin": None,
    "prop": {"over", "under", "yes", "no", "odd", "even"},
}


def market_contract_reason(sport, market, selection, period, stat, entity="all", line=None):
    """Return a stable rejection code unless the complete canonical market is supported.

    The contract is intentionally narrower than the display catalogue: a market
    can be rendered without having provider semantics and settlement support for
    every sport. Baseball first-five markets and tennis set markets remain
    unsupported until the results path can settle those exact periods. Baseball
    first-five totals are the exception, as the provider and settlement paths
    explicitly identify that market.
    """
    if not isinstance(sport, str) or sport not in _SPORT_MARKETS:
        return "unsupported_sport"
    if not isinstance(market, str) or market not in _SPORT_MARKETS[sport]:
        return "unsupported_market_for_sport"
    if not isinstance(selection, str) or not selection:
        return "missing_outcome"
    choices = _OUTCOMES[market]
    if choices is not None:
        if selection not in choices:
            return "invalid_outcome"
    elif market == "ht_ft":
        if not re.fullmatch(r"(?:home|draw|away)/(?:home|draw|away)", selection):
            return "invalid_outcome"
    elif market == "correct_score":
        if not re.fullmatch(r"\d+-\d+", selection):
            return "invalid_outcome"
    elif market == "result_total":
        if not re.fullmatch(r"(?:home|draw|away)_(?:over|under)", selection):
            return "invalid_outcome"
    elif market == "result_btts":
        if not re.fullmatch(r"(?:home|draw|away)_(?:yes|no)", selection):
            return "invalid_outcome"
    elif market == "winning_margin":
        if not re.fullmatch(r"(?:home|away)_\d+(?:[+-]\d+)?", selection):
            return "invalid_outcome"
    if not isinstance(period, str) or period not in _SPORT_PERIODS[sport]:
        return "unsupported_period"
    if sport == "baseball" and period == "1i5" and market != "total":
        return "unsupported_period"
    if not isinstance(stat, str) or not stat:
        return "missing_stat"
    if not isinstance(entity, str) or not entity:
        return "invalid_entity"
    if line is not None and (isinstance(line, bool) or not isinstance(line, (int, float)) or not math.isfinite(line)):
        return "invalid_line"
    if market in {"result3", "result2", "dnb", "dc", "btts", "odd_even", "ht_ft",
                  "correct_score", "result_total", "result_btts", "clean_sheet",
                  "win_to_nil", "team_to_score", "winning_margin"}:
        if stat != "points":
            return "unsupported_stat"
    elif market == "total":
        allowed_stats = ({"points", "goals", "corners", "cards", "shots", "shots_on_target"} if sport == "football"
                         else {"sets", "games"} if sport == "tennis" else {"points"})
        if stat not in allowed_stats:
            return "unsupported_stat"
    elif market == "handicap":
        allowed_stats = ({"points", "corners", "cards"} if sport == "football"
                         else {"games"} if sport == "tennis" else {"points"})
        if stat not in allowed_stats:
            return "unsupported_stat"
    if market in {"total", "handicap", "result_total"} and line is None:
        return "missing_line"
    if market == "prop":
        if stat.lower() not in _PROP_STATS[sport]:
            return "unsupported_stat"
        if selection in {"over", "under"} and line is None:
            return "missing_line"
        if entity == "all":
            return "invalid_entity"
    if market == "total" and entity not in {"all", "home", "away"}:
        return "invalid_entity"
    if market in {"clean_sheet", "win_to_nil", "team_to_score"} and entity not in {"home", "away"}:
        return "invalid_entity"
    if market not in {"total", "clean_sheet", "win_to_nil", "team_to_score", "prop"} and entity != "all":
        return "invalid_entity"
    return None


def ambiguous_contract(market_name):
    name = str(market_name or '').lower().replace('_', ' ')
    # Old Sharp normalization discarded the unknown segment but retained its
    # original market name. Quarantine at evaluation/read boundaries as well
    # as in the raw parser, so a cached row cannot reintroduce the bad contract.
    simple_total = re.fullmatch(r'total ([a-z]+(?: [a-z]+)?)', name)
    unknown_stat = simple_total and simple_total[1] not in {
        'points','goals','runs','games','sets','rounds','corners','cards'}
    return bool(unknown_stat or re.search(r'\b(?:\d+(?:st|nd|rd|th)|first|second|third|fourth) inning\b', name))
