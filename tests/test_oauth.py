from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

import agent_bus.oauth as oauth
from agent_bus.tokens import TokenStore

ISSUER = "https://okta.example/oauth2/default"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/v1/authorize",
    "token_endpoint": f"{ISSUER}/v1/token",
    "userinfo_endpoint": f"{ISSUER}/v1/userinfo",
}


@pytest.fixture
def store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TokenStore:
    token_store = TokenStore(path=tmp_path / "tokens.sqlite")
    monkeypatch.setattr("agent_bus.tokens.get_token_store", lambda: token_store)
    return token_store


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BUS_OKTA_ISSUER", ISSUER)
    monkeypatch.setenv("AGENT_BUS_OKTA_CLIENT_ID", "client-1")
    monkeypatch.setenv("AGENT_BUS_OKTA_CLIENT_SECRET", "secret-1")
    monkeypatch.setenv("AGENT_BUS_PUBLIC_URL", "http://localhost:8080")


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset module caches between tests."""
    monkeypatch.setattr(oauth, "_discovery", None)
    oauth._pending_logins.clear()


def mock_client(
    monkeypatch: pytest.MonkeyPatch, userinfo: dict | None = None
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    default_userinfo = {"sub": "user-1", "email": "a@b.c", "name": "Alice", "groups": []}
    body = userinfo if userinfo is not None else default_userinfo

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        if url == DISCOVERY["token_endpoint"]:
            return httpx.Response(200, json={"access_token": "at-ok"})
        if url == DISCOVERY["userinfo_endpoint"]:
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not mocked"})

    monkeypatch.setattr(
        oauth, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    return requests


# --- env validation -----------------------------------------------------------


def test_missing_env(env, fresh) -> None:
    import os

    saved = os.environ["AGENT_BUS_OKTA_CLIENT_SECRET"]
    del os.environ["AGENT_BUS_OKTA_CLIENT_SECRET"]
    assert oauth.missing_env() == ["AGENT_BUS_OKTA_CLIENT_SECRET"]
    os.environ["AGENT_BUS_OKTA_CLIENT_SECRET"] = saved
    assert oauth.missing_env() == []


# --- start_login ---------------------------------------------------------------


def test_start_login_builds_authorize_url(env, fresh, monkeypatch) -> None:
    mock_client(monkeypatch)
    url = oauth.start_login(browser=False)
    assert url.startswith(DISCOVERY["authorization_endpoint"])
    assert "code_challenge_method=S256" in url
    assert "response_type=code" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fauth%2Fcallback" in url
    assert len(oauth._pending_logins) == 1


def test_start_login_unreachable(env, fresh) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    monkeypatch_handler(handler)
    with pytest.raises(oauth.OktaUnavailable):
        oauth.start_login(browser=False)


def monkeypatch_handler(handler) -> None:
    import pytest as _pytest  # noqa: F401

    oauth._client = lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_discovery_issuer_mismatch(env, fresh) -> None:
    bad = dict(DISCOVERY, issuer="https://evil.example")
    oauth._client = lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=bad))
    )
    with pytest.raises(oauth.OAuthError, match="issuer"):
        oauth.start_login(browser=False)


# --- complete_login -------------------------------------------------------------


def login_and_complete(monkeypatch, store, *, userinfo=None, browser=False):
    mock_client(monkeypatch, userinfo=userinfo)
    url = oauth.start_login(browser=browser)
    state = url.split("state=")[1].split("&")[0]
    return oauth.complete_login(code="auth-code", state=state)


def test_complete_login_mints_token(env, fresh, store, monkeypatch) -> None:
    result = login_and_complete(monkeypatch, store)
    assert result.sub == "user-1"
    assert result.admin is False
    assert result.raw_token.startswith("ab_")
    row = store.lookup(result.raw_token)
    assert row is not None and row["sub"] == "user-1"


def test_complete_login_admin_from_group(env, fresh, store, monkeypatch) -> None:
    result = login_and_complete(
        monkeypatch,
        store,
        userinfo={
            "sub": "admin-1",
            "groups": [oauth.DEFAULT_ADMIN_GROUP],
        },
    )
    assert result.admin is True
    assert store.lookup(result.raw_token)["admin"] == 1


def test_complete_login_missing_groups_warns(env, fresh, store, monkeypatch, caplog) -> None:
    with caplog.at_level("WARNING", logger="agent_bus.oauth"):
        result = login_and_complete(monkeypatch, store, userinfo={"sub": "u2"})
    assert result.admin is False
    assert any("groups claim" in r.message for r in caplog.records)


def test_complete_login_unknown_state(env, fresh, store) -> None:
    with pytest.raises(oauth.OAuthError, match="state"):
        oauth.complete_login(code="c", state="bogus")


def test_complete_login_expired_state(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch)
    url = oauth.start_login(browser=False)
    state = url.split("state=")[1].split("&")[0]
    verifier, _, browser = oauth._pending_logins[state]
    oauth._pending_logins[state] = (verifier, time.time() - 1, browser)
    with pytest.raises(oauth.OAuthError, match="state"):
        oauth.complete_login(code="c", state=state)


def test_complete_login_exchange_failure(env, fresh, store, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(
        oauth, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    url = oauth.start_login(browser=False)
    state = url.split("state=")[1].split("&")[0]
    with pytest.raises(oauth.OktaUnavailable):
        oauth.complete_login(code="bad", state=state)


def test_complete_login_userinfo_missing_sub(env, fresh, store, monkeypatch) -> None:
    with pytest.raises(oauth.OAuthError, match="sub"):
        login_and_complete(monkeypatch, store, userinfo={"email": "a@b.c"})


def test_pending_login_state_is_one_shot(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch)
    url = oauth.start_login(browser=False)
    state = url.split("state=")[1].split("&")[0]
    oauth.complete_login(code="c", state=state)
    # replaying the same state must fail
    with pytest.raises(oauth.OAuthError, match="state"):
        oauth.complete_login(code="c", state=state)


def test_pending_logins_bounded(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch)
    for _ in range(oauth.MAX_PENDING_LOGINS + 10):
        oauth.start_login(browser=False)
    assert len(oauth._pending_logins) <= oauth.MAX_PENDING_LOGINS


# --- HTTP routes ----------------------------------------------------------------


def route_client(monkeypatch, store) -> TestClient:
    from agent_bus.web import server as web_server

    return TestClient(web_server.app)


def test_login_route_redirects(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch)
    client = route_client(monkeypatch, store)
    res = client.get("/auth/login", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"].startswith(DISCOVERY["authorization_endpoint"])


def test_login_route_503_when_unreachable(env, fresh, monkeypatch) -> None:
    oauth._client = lambda: httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))
        )
    )
    from agent_bus.web import server as web_server

    client = TestClient(web_server.app)
    res = client.get("/auth/login")
    assert res.status_code == 503


def test_callback_show_once_page(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch, userinfo={"sub": "user-1", "groups": []})
    from agent_bus.web import server as web_server

    with TestClient(web_server.app) as client:
        login = client.get("/auth/login", follow_redirects=False)
        state = login.headers["location"].split("state=")[1].split("&")[0]
        res = client.get(f"/auth/callback?code=c&state={state}")

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    text = res.text
    raw_prefix = text.split('<code id="tok">')[1].split("</code>")[0]
    assert store.lookup(raw_prefix) is not None  # shown token is valid
    assert "mcpServers" in text


def test_callback_show_once_links_admin_for_group_members(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch, userinfo={"sub": "admin-1", "groups": [oauth.DEFAULT_ADMIN_GROUP]})
    from agent_bus.web import server as web_server

    with TestClient(web_server.app) as client:
        login = client.get("/auth/login", follow_redirects=False)
        state = login.headers["location"].split("state=")[1].split("&")[0]
        res = client.get(f"/auth/callback?code=c&state={state}")

    assert res.status_code == 200
    assert "/auth/login?browser=1" in res.text  # admin link for group members

    # A non-member login shows no admin link.
    mock_client(monkeypatch, userinfo={"sub": "user-1", "groups": []})
    with TestClient(web_server.app) as client:
        login = client.get("/auth/login", follow_redirects=False)
        state = login.headers["location"].split("state=")[1].split("&")[0]
        res = client.get(f"/auth/callback?code=c&state={state}")
    assert res.status_code == 200
    assert "/admin" not in res.text


def test_callback_browser_sets_cookie(env, fresh, store, monkeypatch) -> None:
    mock_client(monkeypatch, userinfo={"sub": "admin-1", "groups": [oauth.DEFAULT_ADMIN_GROUP]})
    from agent_bus.web import server as web_server

    with TestClient(web_server.app) as client:
        login = client.get("/auth/login?browser=1", follow_redirects=False)
        state = login.headers["location"].split("state=")[1].split("&")[0]
        res = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/admin"
    set_cookie = res.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    cookie_value = set_cookie.split("agent_bus_token=")[1].split(";")[0]
    row = store.lookup(cookie_value)
    assert row is not None and row["admin"] == 1 and row["browser"] == 1
