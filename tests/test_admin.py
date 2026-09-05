from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_bus.tokens import TokenStore
from agent_bus.web import server as web_server

ISS = "https://test.local"
ORIGIN = "http://localhost:8080"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokenStore:
    token_store = TokenStore(path=tmp_path / "tokens.sqlite")
    monkeypatch.setattr("agent_bus.tokens.get_token_store", lambda: token_store)
    monkeypatch.setattr("agent_bus.oauth._client", lambda: token_store)  # unused, keep isolated
    return token_store


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BUS_PUBLIC_URL", ORIGIN)


def browser_admin_cookie(store: TokenStore) -> str:
    _, raw = store.mint(iss=ISS, sub="ops", admin=True, browser=True)
    return raw


def test_admin_page_requires_browser_admin(store, env) -> None:
    _, mcp_admin = store.mint(iss=ISS, sub="ops", admin=True)  # MCP token: never admin
    _, browser_pleb = store.mint(iss=ISS, sub="alice", browser=True)
    cookie = browser_admin_cookie(store)

    with TestClient(web_server.app) as client:
        # no token at all: browsers are redirected to the admin login flow
        res = client.get("/admin", follow_redirects=False)
        assert res.status_code in (302, 307)
        assert res.headers["location"] == "/auth/login?browser=1"

        assert client.get("/admin", cookies={"agent_bus_token": cookie}).status_code == 200
        assert "admin" in client.get("/admin", cookies={"agent_bus_token": cookie}).text.lower()
        # admin group but MCP-minted -> forbidden
        res = client.get("/admin", headers={"Authorization": f"Bearer {mcp_admin}"})
        assert res.status_code == 403
        # browser session without admin group -> forbidden
        assert client.get("/admin", cookies={"agent_bus_token": browser_pleb}).status_code == 403


def test_api_admin_still_requires_token(store, env) -> None:
    # /admin the page became public (redirects), but data endpoints stay gated:
    # no token -> 401 from the middleware, not a redirect.
    from fastapi.testclient import TestClient

    with TestClient(web_server.app) as client:
        res = client.get("/api/admin/tokens")
        assert res.status_code == 401


def test_admin_tokens_list_has_no_hashes(store, env) -> None:
    store.mint(iss=ISS, sub="alice")
    cookie = browser_admin_cookie(store)

    with TestClient(web_server.app) as client:
        res = client.get("/api/admin/tokens", cookies={"agent_bus_token": cookie})
    assert res.status_code == 200
    tokens = res.json()["tokens"]
    assert len(tokens) == 2
    assert all("token_hash" not in t and "raw" not in t for t in tokens)
    assert {t["sub"] for t in tokens} == {"alice", "ops"}


def test_revoke_via_cookie_needs_same_origin(store, env) -> None:
    cookie = browser_admin_cookie(store)
    victim_id, _ = store.mint(iss=ISS, sub="alice")

    with TestClient(web_server.app) as client:
        no_origin = client.post(
            f"/api/admin/tokens/{victim_id}/revoke", cookies={"agent_bus_token": cookie}
        )
        assert no_origin.status_code == 403

        cross = client.post(
            f"/api/admin/tokens/{victim_id}/revoke",
            cookies={"agent_bus_token": cookie},
            headers={"Origin": "https://evil.example"},
        )
        assert cross.status_code == 403

        ok = client.post(
            f"/api/admin/tokens/{victim_id}/revoke",
            cookies={"agent_bus_token": cookie},
            headers={"Origin": ORIGIN},
        )
        assert ok.status_code == 200

    assert store.valid(victim_id) is False


def test_revoke_via_bearer_skips_origin_check(store, env) -> None:
    # Bearer-authenticated MCP/CLI admin tokens are CSRF-immune... but admin
    # is browser-only, so Bearer admins are rejected on the admin flag instead.
    _, mcp_admin = store.mint(iss=ISS, sub="ops", admin=True)
    victim_id, _ = store.mint(iss=ISS, sub="alice")

    with TestClient(web_server.app) as client:
        res = client.post(
            f"/api/admin/tokens/{victim_id}/revoke",
            headers={"Authorization": f"Bearer {mcp_admin}"},
        )
    assert res.status_code == 403
    assert store.valid(victim_id) is True
