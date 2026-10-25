"""Outlier-robust cross-book average used to de-vig a market (predbot/model/ensemble.py).

Regression for 2026-10-24: the Pro handicap picks (DC United +0.5, Colorado +0.5) had positive EV only because
one bookmaker's quote (1.23 / 1.22 on a line the others priced at ~1.65) pulled the plain average and so the
market probability up. The quote sets below are the real ones from the production candidate pool."""
import pytest

from predbot.model.devig import devig
from predbot.model.ensemble import OUTLIER_BOOK_TOLERANCE, EventModel
from predbot.providers.base import MarketLine


def _ml(selection, line, book_prices):
    return MarketLine(market="handicap", selection=selection, price=book_prices[0], period="full",
                      entity="all", stat="points", line=line, book_prices=list(book_prices))


DC_HOME = [1.2273, 1.6667, 1.625, 1.6667]   # DC United +0.5; FanDuel 1.2273 is the outlier
DC_AWAY = [3.9, 2.25, 2.2]                  # Cincinnati -0.5; FanDuel 3.9 is the outlier


def test_tolerance_is_a_quarter_of_the_median():
    assert OUTLIER_BOOK_TOLERANCE == 0.25


def test_low_outlier_is_dropped_before_averaging():
    home = _ml("home", 0.5, DC_HOME)
    mean = EventModel._mean_price(home)
    assert mean == pytest.approx((1.6667 + 1.625 + 1.6667) / 3, abs=1e-6)


def test_high_outlier_is_dropped_before_averaging():
    away = _ml("away", -0.5, DC_AWAY)
    assert EventModel._mean_price(away) == pytest.approx((2.25 + 2.2) / 2, abs=1e-6)


def test_fewer_than_three_quotes_are_never_judged_against_a_median():
    two = _ml("home", 0.5, [1.2273, 1.65])
    assert EventModel._mean_price(two) == pytest.approx((1.2273 + 1.65) / 2, abs=1e-6)


def test_clean_market_is_unchanged():
    clean = _ml("home", 0.5, [1.6, 1.62, 1.65])
    assert EventModel._mean_price(clean) == pytest.approx((1.6 + 1.62 + 1.65) / 3, abs=1e-6)


def test_outlier_no_longer_inflates_the_market_probability():
    """The DC United +0.5 market probability with the outlier kept was ~0.634 (what Pro saw). With it dropped,
    the de-vigged probability falls to ~0.57, which is below Bet of the Day's 0.62 floor."""
    with_outlier = devig({"home": sum(DC_HOME) / len(DC_HOME), "away": sum(DC_AWAY) / len(DC_AWAY)})["home"]
    robust = devig({"home": EventModel._mean_price(_ml("home", 0.5, DC_HOME)),
                    "away": EventModel._mean_price(_ml("away", -0.5, DC_AWAY))})["home"]
    assert with_outlier > 0.60
    assert robust < 0.60
    assert robust == pytest.approx(0.575, abs=0.01)
