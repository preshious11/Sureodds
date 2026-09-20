"""After a provider merge the kept event carries, per side, the FULLER of the providers' team names - so a card says
'Boston Red Sox', not 'BOS Red Sox' - using the pairs from CROSS_TIER_REPORT.md."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from predbot.bot.format import outcome_label
from predbot.providers.base import Event, MarketLine
from predbot.providers.chain import _fuller, merge_events

START = datetime(2030, 6, 1, 23, 10, tzinfo=timezone.utc)


def ev(provider, home, away, sport="baseball", price=1.6, selection="home"):
    ml = MarketLine("result2", selection, price, n_books=1, book_prices=[price])
    return Event(provider=provider, provider_event_id=f"{provider}-1", sport=sport, league="MLB", home=home, away=away,
                 start=START, markets=[ml], sources=[provider])


@pytest.mark.parametrize("first,second,home,away", [
    (("Tampa Bay Rays", "BOS Red Sox"), ("Tampa Bay Rays", "Boston Red Sox"), "Tampa Bay Rays", "Boston Red Sox"),
    (("Texas Rangers", "TOR Blue Jays"), ("Texas Rangers", "Toronto Blue Jays"), "Texas Rangers", "Toronto Blue Jays"),
    (("Cleveland Guardians", "Athletics"), ("Cleveland Guardians", "Oakland Athletics"),
     "Cleveland Guardians", "Oakland Athletics"),
    (("ARI Diamondbacks", "New York Yankees"), ("Arizona Diamondbacks", "New York Yankees"),
     "Arizona Diamondbacks", "New York Yankees"),
])
def test_the_kept_event_carries_the_fuller_name_on_each_side(first, second, home, away):
    (merged,) = merge_events([ev("sharpapi", *first)], [ev("sportsgameodds", *second)])
    assert (merged.home, merged.away) == (home, away)
    assert merged.provider == "sharpapi"                                  # still the first provider's event


def test_the_order_of_the_providers_does_not_change_the_result():
    (a,) = merge_events([ev("sharpapi", "Cleveland Guardians", "Athletics")],
                        [ev("sportsgameodds", "Cleveland Guardians", "Oakland Athletics")])
    (b,) = merge_events([ev("sportsgameodds", "Cleveland Guardians", "Oakland Athletics")],
                        [ev("sharpapi", "Cleveland Guardians", "Athletics")])
    assert (a.home, a.away) == (b.home, b.away) == ("Cleveland Guardians", "Oakland Athletics")


def test_a_swapped_provider_is_compared_side_for_side():
    (merged,) = merge_events([ev("sharpapi", "TEX Rangers", "BOS Red Sox")],
                             [ev("sportsgameodds", "Boston Red Sox", "Texas Rangers", selection="away")])
    assert (merged.home, merged.away) == ("Texas Rangers", "Boston Red Sox")     # TEX -> Texas on the home side
    assert len(merged.markets) == 1 and merged.markets[0].n_books == 2           # and it was ONE bet


def test_the_pick_text_uses_the_fuller_name():
    (merged,) = merge_events([ev("sharpapi", "Cleveland Guardians", "Athletics")],
                             [ev("sportsgameodds", "Cleveland Guardians", "Oakland Athletics")])
    ml = MarketLine("result2", "away", 1.6, n_books=2)
    assert outcome_label("", ml, merged.home, merged.away, "baseball") == "Oakland Athletics to Win"


def test_the_choice_rule():
    assert _fuller("Athletics", "Oakland Athletics") == "Oakland Athletics"        # more words
    assert _fuller("BOS Red Sox", "Boston Red Sox") == "Boston Red Sox"            # equal words: the longer
    assert _fuller("Boston Red Sox", "BOS Red Sox") == "Boston Red Sox"
    assert _fuller("Arsenal", "Arsenal") == "Arsenal"                              # equal: keep the first
    assert _fuller("Man United", "Manchester United") == "Manchester United"
    assert _fuller("Real Madrid", "Real Madrid CF") == "Real Madrid CF"
    for a, b in (("Athletics", "Oakland Athletics"), ("BOS Red Sox", "Boston Red Sox")):
        assert _fuller(a, b) in (a, b)                                             # never a blend
