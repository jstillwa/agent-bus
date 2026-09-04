from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_bus.auth import AuthIdentity, TokenAuthMiddleware, identity_from_scope
from agent_bus.tokens import TokenStore

API_KEY_HEADER = {"x-api-key": None}


def make_app() -> tuple[Starlette, dict]:
    captured: dict = {}

    async def me(request):
        captured["state"] = dict(request.scope.get("state") or {})
        return JSONResponse({"auth": getattr(request.state, "auth", None) is not None})

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[Route("/me", me), Route("/health", health), Route("/auth/login", me)],
    )
    app.add_middleware(TokenAuthMiddleware)
    return app, captured


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    store = TokenStore(path=tmp_path / "tokens.sqlite")
    monkeypatch.setattr("agent_bus.tokens.get_token_store", lambda: store)
    app, _ = make_app()
    return TestClient(app)


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.sqlite")


def minted(store: TokenStore, **kwargs) -> tuple[str, str]:
    defaults = {"iss": "https://okta.example", "sub": "user-1"}
    return store.mint(**{**defaults, **kwargs})


def test_bearer_token_passes(client: TestClient, store: TokenStore) -> None:
    _, raw = minted(store)
    res = client.get("/me", headers={"Authorization": f"Bearer {raw}"})
    assert res.status_code == 200
    assert res.json()["auth"] is True


def test_api_key_header_passes(client: TestClient, store: TokenStore) -> None:
    _, raw = minted(store)
    res = client.get("/me", headers={"X-API-Key": raw})
    assert res.status_code == 200


def test_cookie_passes(client: TestClient, store: TokenStore) -> None:
    _, raw = minted(store)
    res = client.get("/me", cookies={"agent_bus_token": raw})
    assert res.status_code == 200


def test_query_param_token_rejected(client: TestClient, store: TokenStore) -> None:
    _, raw = minted(store)
    res = client.get("/me", params={"token": raw})
    assert res.status_code == 401


def test_missing_token_401(client: TestClient) -> None:
    res = client.get("/me")
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"
    assert "login_url" in res.json()


def test_revoked_token_401(client: TestClient, store: TokenStore) -> None:
    token_id, raw = minted(store)
    store.revoke(token_id)
    res = client.get("/me", headers={"Authorization": f"Bearer {raw}"})
    assert res.status_code == 401


def test_public_paths_bypass(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/auth/login").status_code == 200


def test_identity_in_state(client: TestClient, store: TokenStore, monkeypatch) -> None:
    app, captured = make_app()
    _ = monkeypatch  # store fixture client unused; fresh app captures state
    monkeypatch.setattr("agent_bus.tokens.get_token_store", lambda: store)
    test_client = TestClient(app)
    _, raw = minted(store, admin=True, browser=True)
    res = test_client.get("/me", headers={"Authorization": f"Bearer {raw}"})
    assert res.status_code == 200
    identity = captured["state"]["auth"]
    assert isinstance(identity, AuthIdentity)
    assert identity.sub == "user-1"
    assert identity.admin is True
    assert identity.browser is True


def test_identity_from_scope_none_cases() -> None:
    assert identity_from_scope(None) is None
    assert identity_from_scope({}) is None
    assert identity_from_scope({"state": {}}) is None
    assert identity_from_scope({"state": {"auth": "not-identity"}}) is None
