"""The shadow's SportyBet read can only fetch the upcoming-events catalogue, and only within its budget."""
import pytest

from predbot.pro_value import sporty_catalogue as sc


@pytest.mark.parametrize("method,path", [
    ("POST", sc.CATALOGUE_PATH),
    ("GET", "/orders/share/ABCD12"),
    ("POST", "/orders/share"),
    ("GET", "/factsCenter/other"),
])
def test_only_the_catalogue_get_is_allowed(method, path):
    transport = sc.CatalogueTransport(limit=5, total_timeout=30)
    with pytest.raises(sc.CatalogueError) as err:
        transport.request(method, path)
    assert err.value.status == "forbidden_route"
    assert transport.used == 0  # refused before any network use


def test_request_budget_is_enforced_before_the_network(monkeypatch):
    transport = sc.CatalogueTransport(limit=1, total_timeout=30)
    transport.used = 1
    with pytest.raises(sc.CatalogueError) as err:
        transport.request("GET", sc.CATALOGUE_PATH)
    assert err.value.status == "request_budget_exhausted"


def test_envelope_accepts_only_the_success_shape():
    assert sc.envelope({"bizCode": 10000, "data": {"tournaments": []}}) == {"tournaments": []}
    for bad in ({"bizCode": 500, "data": {}}, {"bizCode": 10000, "data": []}, ["x"]):
        with pytest.raises(sc.CatalogueError):
            sc.envelope(bad)


def test_bad_timeout_is_refused():
    with pytest.raises(sc.CatalogueError):
        sc.CatalogueTransport(total_timeout=0)


class _FakeResponse:
    def __init__(self, status, body=b"", content_type="application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def iter_content(self, _size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    calls = []
    response = None

    def __init__(self):
        self.auth = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        _FakeSession.calls.append((url, self.auth, kwargs))
        return _FakeSession.response


def test_catalogue_get_is_anonymous_and_never_follows_redirects(monkeypatch):
    _FakeSession.calls = []
    _FakeSession.response = _FakeResponse(200, b'{"bizCode": 10000, "data": {}}')
    monkeypatch.setattr(sc.requests, "Session", _FakeSession)
    data = sc.CatalogueTransport(limit=2, total_timeout=30).request("GET", sc.CATALOGUE_PATH, params={"pageNum": 1})
    assert data == {"bizCode": 10000, "data": {}}
    url, auth, kwargs = _FakeSession.calls[0]
    assert url == sc.BASE + sc.CATALOGUE_PATH
    assert isinstance(auth, sc.AnonymousAuth)
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert "Cookie" not in kwargs["headers"] and "Authorization" not in kwargs["headers"]


@pytest.mark.parametrize("status,error", [(401, "access_blocked"), (403, "access_blocked"),
                                          (429, "rate_limited"), (500, "http_error")])
def test_upstream_refusals_map_to_fixed_statuses(monkeypatch, status, error):
    _FakeSession.response = _FakeResponse(status)
    monkeypatch.setattr(sc.requests, "Session", _FakeSession)
    with pytest.raises(sc.CatalogueError) as err:
        sc.CatalogueTransport(limit=2, total_timeout=30).request("GET", sc.CATALOGUE_PATH)
    assert err.value.status == error


def test_non_json_and_oversized_bodies_are_refused(monkeypatch):
    _FakeSession.response = _FakeResponse(200, b"<html>", content_type="text/html")
    monkeypatch.setattr(sc.requests, "Session", _FakeSession)
    with pytest.raises(sc.CatalogueError) as err:
        sc.CatalogueTransport(limit=2, total_timeout=30).request("GET", sc.CATALOGUE_PATH)
    assert err.value.status == "non_json_response"
    _FakeSession.response = _FakeResponse(200, b" " * (sc.MAX_BYTES + 1))
    with pytest.raises(sc.CatalogueError) as err:
        sc.CatalogueTransport(limit=2, total_timeout=30).request("GET", sc.CATALOGUE_PATH)
    assert err.value.status == "response_too_large"
