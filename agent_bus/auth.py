from __future__ import annotations

import os
import urllib.parse
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def get_auth_secret() -> str | None:
    """Retrieve the shared secret from environment variables."""
    token = os.environ.get("AGENT_BUS_AUTH_TOKEN") or os.environ.get("AGENT_BUS_SECRET")
    if token:
        token = token.strip()
    return token or None


class SharedSecretAuthMiddleware:
    """ASGI middleware providing shared-secret authentication.

    Supports:
    - Authorization: Bearer <secret>
    - X-API-Key: <secret>
    - Query parameter: ?token=<secret>
    - Cookie: agent_bus_token=<secret>

    If AGENT_BUS_AUTH_TOKEN is not set, all requests are permitted.
    The /health endpoint is always permitted for healthchecks.
    """

    def __init__(self, app: Any, secret: str | None = None) -> None:
        self.app = app
        self.secret = secret if secret is not None else get_auth_secret()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.secret:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        query_string = scope.get("query_string", b"").decode("latin-1")
        query_params = urllib.parse.parse_qs(query_string)

        token: str | None = None

        # 1. Authorization: Bearer <token>
        auth_header = headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif auth_header:
            token = auth_header.strip()

        # 2. X-API-Key: <token>
        if not token:
            token = headers.get("x-api-key")

        # 3. Query param: ?token=<token>
        query_token = query_params.get("token", [None])[0]
        if not token and query_token:
            token = query_token

        # 4. Cookie: agent_bus_token=<token>
        if not token:
            cookie_header = headers.get("cookie", "")
            for cookie in cookie_header.split(";"):
                cookie = cookie.strip()
                if cookie.startswith("agent_bus_token="):
                    token = cookie.split("=", 1)[1]
                    break

        if token != self.secret:
            response = JSONResponse(
                {"error": "Unauthorized", "detail": "Valid authentication token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # If authenticated via query parameter, set cookie on response for browser convenience
        if query_token and query_token == self.secret:

            async def send_with_cookie(message: dict[str, Any]) -> None:
                if message["type"] == "http.response.start":
                    res_headers = list(message.get("headers", []))
                    cookie_val = (
                        f"agent_bus_token={self.secret}; Path=/; HttpOnly; SameSite=Lax"
                    ).encode("latin-1")
                    res_headers.append((b"set-cookie", cookie_val))
                    message["headers"] = res_headers
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return

        await self.app(scope, receive, send)
