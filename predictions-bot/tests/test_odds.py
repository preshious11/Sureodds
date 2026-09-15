"""Odds conversions, implied probability, de-vig."""
from __future__ import annotations

import math

from predbot.model.devig import devig, single_side_prob, blend_logit, logit, sigmoid
from predbot.util.odds import (american_to_decimal, decimal_to_american,
                               implied_prob, fair_decimal, parse_decimal)


def test_american_to_decimal():
    assert american_to_decimal("+150") == 2.5
    assert american_to_decimal(150) == 2.5
    assert american_to_decimal(-200) == 1.5
    assert american_to_decimal("-110") == round(1 + 100 / 110, 4)
    assert american_to_decimal(0) is None
    assert american_to_decimal("abc") is None
    assert american_to_decimal(None) is None


def test_decimal_american_roundtrip():
    for a in (-250, -110, 120, 300):
        d = american_to_decimal(a)
        assert abs(decimal_to_american(d) - a) <= 1


def test_implied_and_fair():
    assert abs(implied_prob(2.0) - 0.5) < 1e-9
    assert implied_prob(1.0) == 0.0
    assert abs(fair_decimal(0.5) - 2.0) < 1e-9
    # fair_decimal never divides by zero at the boundaries
    assert fair_decimal(0.0) > 1
    assert fair_decimal(1.0) > 1


def test_parse_decimal_rejects_non_odds():
    assert parse_decimal("2.5") == 2.5
    assert parse_decimal(1.0) is None      # a decimal <= 1 is not a valid price
    assert parse_decimal("junk") is None
    assert parse_decimal(None) is None


def test_devig_two_way_sums_to_one():
    fair = devig({"home": 1.90, "away": 1.90})
    assert abs(sum(fair.values()) - 1.0) < 1e-9
    assert abs(fair["home"] - 0.5) < 1e-6   # symmetric market -> 50/50


def test_devig_strips_margin():
    # a book price of 1.90/1.90 implies 1.053 total; fair prob must be < implied
    fair = devig({"home": 1.90, "away": 1.90})
    assert fair["home"] < implied_prob(1.90)


def test_devig_three_way():
    fair = devig({"home": 2.10, "draw": 3.40, "away": 3.80})
    assert abs(sum(fair.values()) - 1.0) < 1e-9
    assert fair["home"] > fair["draw"] > fair["away"]


def test_single_side_prob_below_raw_implied():
    assert single_side_prob(2.0, ways=2) < implied_prob(2.0)


def test_logit_sigmoid_roundtrip():
    for p in (0.1, 0.5, 0.83):
        assert abs(sigmoid(logit(p)) - p) < 1e-9


def test_blend_logit_weighted():
    p = blend_logit({"a": 0.6, "b": 0.6}, {"a": 1.0, "b": 1.0})
    assert abs(p - 0.6) < 1e-6
    assert blend_logit({"a": 0.6}, {"a": 0.0}) is None   # no weight -> None
