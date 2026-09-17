"""SGO cross-provider settlement: the free tier needs a leagueID on event queries."""
from datetime import datetime, timezone
from unittest.mock import Mock

from predbot.providers.sportsgameodds import SPORT_TO_SGO, SportsGameOddsProvider

NOW = datetime(2030, 6, 1, 18, 0, tzinfo=timezone.utc)


def test_find_result_scopes_to_configured_leagues(budget):
    p = SportsGameOddsProvider('k', budget, ['SOCCER', 'BASEBALL'], ['MLB', 'MLS'])
    p.http = Mock()
    p.http.get.return_value = {'data': []}
    p.find_result('baseball', 'Cubs', 'Dodgers', NOW)
    params = p.http.get.call_args.args[1]
    assert params['leagueID'] == 'MLB,MLS'
    assert params['sportID'] == SPORT_TO_SGO['baseball']


def test_find_result_without_leagues_stays_sport_scoped(budget):
    p = SportsGameOddsProvider('k', budget, ['BASEBALL'], [])
    p.http = Mock()
    p.http.get.return_value = {'data': []}
    p.find_result('baseball', 'Cubs', 'Dodgers', NOW)
    assert 'leagueID' not in p.http.get.call_args.args[1]
