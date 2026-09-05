"""Token authentication middleware for the Agent Bus HTTP deployment.

Every HTTP request must present a token minted via the Okta login flow
(or the admin CLI): `Authorization: Bearer <token>`, `X-API-Key: <token>`,
or the `agent_bus_token` cookie (browser sessions). On success the
authenticated identity is attached to `scope["state"]["auth"]`; MCP tools
and web handlers authorize from there. stdio transport never passes through
this middleware (decision: stdio is local-file trust).

The old shared-secret auth (`AGENT_BUS_AUTH_TOKEN`) was deleted; Okta
tokens are the only HTTP auth path. `?token=` query-param auth is
intentionally not supported (credentials must not leak into logs/history).
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

# Paths reachable without a token: healthchecks, the login flow, and the admin
# page itself (which redirects browsers to the login flow; its data endpoints
# /api/admin/* stay token-gated).
PUBLIC_PATHS = ("/health",)
PUBLIC_PREFIXES = ("/auth/",)
# Authenticated-optional paths: identity is attached when a valid token is
# present, but no 401 is raised otherwise (the route redirects browsers to
# the login flow itself).
OPTIONAL_PATHS = ("/admin",)

COOKIE_NAME = "agent_bus_token"


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    iss: str
    sub: str
    admin: bool
    browser: bool
    token_id: str


def login_url() -> str:
    public = os.environ.get("AGENT_BUS_PUBLIC_URL")
    return f"{public.rstrip('/')}/auth/login" if public else "/auth/login"


def _cookie_token(headers: Headers) -> str | None:
    for cookie in headers.get("cookie", "").split(";"):
        cookie = cookie.strip()
        if cookie.startswith(f"{COOKIE_NAME}="):
            return cookie.split("=", 1)[1]
    return None


class TokenAuthMiddleware:
    """ASGI middleware authenticating requests against the token store.

    The store is resolved lazily per request via `tokens.get_token_store()`
    so tests (and the CLI) can inject a store before the server starts.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        token = _extract_token(Headers(scope=scope))
        identity: AuthIdentity | None = None
        if token:
            from agent_bus.tokens import get_token_store

            row = get_token_store().lookup(token)
            if row is not None:
                # Effective admin = admin AND browser: the flag is only honored
                # on browser-minted (24h) tokens so a stale Okta group
                # membership is bounded to 24h (decision). MCP/CLI tokens are
                # never admin, even if the row says so.
                browser = bool(row["browser"])
                identity = AuthIdentity(
                    iss=row["iss"],
                    sub=row["sub"],
                    admin=bool(row["admin"]) and browser,
                    browser=browser,
                    token_id=row["id"],
                )

        if identity is None and path not in OPTIONAL_PATHS:
            response = JSONResponse(
                {
                    "error": "Unauthorized",
                    "detail": "A valid Agent Bus token is required.",
                    "login_url": login_url(),
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})
        scope["state"]["auth"] = identity
        await self.app(scope, receive, send)


def _extract_token(headers: Headers) -> str | None:
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    if auth_header:
        return auth_header.strip()
    api_key = headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    return _cookie_token(headers)


def identity_from_scope(scope: MutableMapping[str, Any] | None) -> AuthIdentity | None:
    """Read the authenticated identity from a raw ASGI scope, if present."""
    if scope is None:
        return None
    state = scope.get("state")
    if not state:
        return None
    identity = state.get("auth")
    return identity if isinstance(identity, AuthIdentity) else None
