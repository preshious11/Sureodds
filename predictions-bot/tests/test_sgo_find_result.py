"""SGO cross-provider settlement: the free tier needs a leagueID on event queries."""
from datetime import datetime, timezone
from unittest.mock import Mock

from predbot.providers.sportsgameodds import SPORT_TO_SGO, SportsGameOddsProvider

NOW = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)


def test_find_result_scopes_to_configured_leagues(budget):
    p = SportsGameOddsProvider('k', budget, ['SOCCER', 'BASEBALL'], ['MLB', 'MLS'])
    p.http = Mock()
    p._available_leagues = {'MLB', 'MLS'}          # tier covers everything configured
    p.http.get.return_value = {'data': []}
    p.find_result('baseball', 'Cubs', 'Dodgers', NOW)
    params = p.http.get.call_args.args[1]
    assert params['leagueID'] == 'MLB,MLS'
    assert params['sportID'] == SPORT_TO_SGO['baseball']


def test_find_result_drops_leagues_outside_the_tier(budget):
    """An out-of-tier league 400s the whole query - it must be dropped first."""
    p = SportsGameOddsProvider('k', budget, ['SOCCER'], ['EPL', 'MLS'])
    p.http = Mock()
    p._available_leagues = {'MLS'}
    p.http.get.return_value = {'data': []}
    p.find_result('football', 'Arsenal', 'Chelsea', NOW)
    assert p.http.get.call_args.args[1]['leagueID'] == 'MLS'


def test_find_result_skips_lookup_when_no_configured_league_is_in_tier(budget):
    p = SportsGameOddsProvider('k', budget, ['SOCCER'], ['EPL'])
    p.http = Mock()
    p._available_leagues = {'MLS'}
    assert p.find_result('football', 'Arsenal', 'Chelsea', NOW) is None
    p.http.get.assert_not_called()                 # no /events call, no 400 spam


def test_find_result_without_leagues_stays_sport_scoped(budget):
    p = SportsGameOddsProvider('k', budget, ['BASEBALL'], [])
    p.http = Mock()
    p.http.get.return_value = {'data': []}
    p.find_result('baseball', 'Cubs', 'Dodgers', NOW)
    assert 'leagueID' not in p.http.get.call_args.args[1]

