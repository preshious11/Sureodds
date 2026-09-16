import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from predbot.cache.manager import CacheManager


def test_configured_redis_is_used_and_round_trips(monkeypatch, caplog):
    client = Mock()
    values = {}
    client.set.side_effect = lambda key, value, **kwargs: values.update({key: value})
    client.get.side_effect = values.get
    factory = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, 'redis', SimpleNamespace(Redis=SimpleNamespace(from_url=factory)))
    url = 'redis://:private-password@host:6379/0'
    with caplog.at_level(logging.INFO):
        cache = CacheManager(redis_url=url)
    assert cache.backend_name == 'redis'
    client.ping.assert_called_once()
    cache.set('test', {'picks': [1]}, 60)
    assert cache.get('test') == {'picks': [1]}
    assert 'connection verified' in caplog.text
    assert 'private-password' not in caplog.text


def test_failed_redis_reports_fallback_without_credentials(monkeypatch, tmp_path, caplog):
    factory = Mock(side_effect=ConnectionError('redis://:private-password@host'))
    monkeypatch.setitem(sys.modules, 'redis', SimpleNamespace(Redis=SimpleNamespace(from_url=factory)))
    with caplog.at_level(logging.WARNING):
        cache = CacheManager(redis_url='redis://host', cache_dir=str(tmp_path))
    assert cache.backend_name == 'file'
    assert 'Redis caching is inactive' in caplog.text
    assert 'ConnectionError' in caplog.text
    assert 'private-password' not in caplog.text
    cache.set('test', {'picks': [1]}, 60)
    assert cache.get('test') == {'picks': [1]}
