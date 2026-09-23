"""SharpAPI feed-shape regressions: matches over futures, honest sport coverage."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from predbot.cache.providers import expected_sports
from predbot.providers.sharpapi import SharpApiProvider, sport_for_league
from tests.conftest import dt


def row(event_id, start, away='Getafe', home='Real Betis', mtype='moneyline'):
    return {'id': f'{event_id}-{mtype}', 'event_id': event_id, 'sport': 'soccer',
            'league': 'spain_-_la_liga', 'home_team': home, 'away_team': away,
            'event_start_time': start, 'market_type': mtype, 'selection_type': 'home',
            'odds_decimal': 2.05}


def feed(pages, paginate=True):
    data = [{'data': batch, 'pagination': {'has_more': i + 1 < len(pages) and paginate,
                                           'next_cursor': f'c{i+1}'}}
            for i, batch in enumerate(pages)]
    http = Mock()
    http.get.side_effect = data
    return http


def test_global_soccer_feed_stops_at_window_edge(budget):
    """The kick-off ordered feed is walked only while the window can still be reached."""
    later = (dt() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%MZ')
    http = feed([[row('e1', later), row('e2', later)]])
    p = SharpApiProvider('k', budget, [], sports=['soccer'], max_pages=6)
    p.http = http
    events = p.fetch_events(dt(), dt(hh=23))
    assert events == []
    assert http.get.call_count == 1      # past-window page 1 ended the walk
    assert all(c.args[1].get('market') == 'moneyline,spread,total'
               for c in http.get.call_args_list)


def test_match_rows_within_window_are_kept_and_outrights_dropped(budget):
    http = feed([[row('e1', '2030-06-01T19:00Z'),
                  row('outrights', '2030-06-01T19:00Z', away='', home='English Premier League',
                      mtype='outright')]])
    p = SharpApiProvider('k', budget, [], sports=['soccer'])
    p.http = http
    events = p.fetch_events(dt(), dt(hh=23))
    assert [e.name for e in events] == ['Real Betis vs Getafe']
    assert events[0].sport == 'football'


def test_per_target_event_budget_and_page_cap(budget):
    starts = (dt() + timedelta(days=3)).strftime('%Y-%m-%dT%H:%MZ')  # outside window
    http = feed([[row(f'e{i}', starts) for i in range(50)]])
    p = SharpApiProvider('k', budget, [], sports=['soccer'], events_per_target=40)
    p.http = http
    p.fetch_events(dt(), dt(hh=23))
    assert http.get.call_count == 1      # 50 priced matches >= 40 -> walk done


def test_expected_sports_reflects_soccer_config(db):
    cfg_leagues = SharpApiProvider('k', Mock(), ['spain_-_la_liga', 'usa_-_major_league_soccer', 'nba'])
    assert expected_sports('sharpapi', cfg_leagues) == ['basketball', 'football']
    assert sport_for_league('table_tennis_-_tt_cup') == 'other'
