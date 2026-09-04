"""Okta (OIDC) Authorization Code + PKCE login flow minting Agent Bus tokens.

Flow: `/auth/login` redirects to the provider; `/auth/callback` exchanges the
code, calls `userinfo` server-to-server (TLS is the trust anchor — no JWT
verification), checks the admin group claim, and mints an Agent Bus token.

- Discovery is fetched lazily on first login and cached; the provider being
  unreachable does not prevent the server from starting or existing tokens
  from working — only `/auth/*` returns 503 while degraded.
- OAuth `state` + PKCE verifier live in a bounded in-memory dict (TTL 10 min,
  one-time consumption). Single-process constraint: same as the rest of the
  server (agent identities are process-local by design).
- Okta does not emit the `groups` claim at `userinfo` by default — the app
  must be configured for it (see docs/okta-setup). A missing claim is logged
  loudly at mint time: without it nobody can ever mint an admin token.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("agent_bus.oauth")

STATE_TTL_SECONDS = 10 * 60
MAX_PENDING_LOGINS = 100
DEFAULT_SCOPES = "openid profile email"
DEFAULT_ADMIN_GROUP = "Permission - agent-bus - Admin"
REQUEST_TIMEOUT_SECONDS = 15.0

REQUIRED_ENV_VARS = (
    "AGENT_BUS_OKTA_ISSUER",
    "AGENT_BUS_OKTA_CLIENT_ID",
    "AGENT_BUS_OKTA_CLIENT_SECRET",
    "AGENT_BUS_PUBLIC_URL",
)


class OAuthError(Exception):
    """Login flow failed (bad state, exchange failure, userinfo failure)."""


class OktaUnavailable(OAuthError):
    """Provider unreachable/degraded — server keeps serving existing tokens."""


def missing_env() -> list[str]:
    return [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]


def public_url() -> str:
    return os.environ["AGENT_BUS_PUBLIC_URL"].rstrip("/")


def redirect_uri() -> str:
    return f"{public_url()}/auth/callback"


def admin_group() -> str:
    return os.environ.get("AGENT_BUS_ADMIN_GROUP") or DEFAULT_ADMIN_GROUP


def scopes() -> str:
    return os.environ.get("AGENT_BUS_OKTA_SCOPES") or DEFAULT_SCOPES


# --- discovery ----------------------------------------------------------------

_discovery: dict[str, Any] | None = None


def _client() -> httpx.Client:
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True)


def get_discovery() -> dict[str, Any]:
    """Fetch and cache the provider discovery document (lazy, validated)."""
    global _discovery
    if _discovery is not None:
        return _discovery
    issuer = os.environ["AGENT_BUS_OKTA_ISSUER"].rstrip("/")
    try:
        response = _client().get(f"{issuer}/.well-known/openid-configuration")
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise OktaUnavailable(f"Login is unavailable: cannot reach issuer ({e})") from e
    document = response.json()
    if document.get("issuer", "").rstrip("/") != issuer:
        raise OAuthError("Discovery document issuer does not match AGENT_BUS_OKTA_ISSUER")
    _discovery = document
    return _discovery


# --- pending logins (state + PKCE verifier, in memory, bounded) ----------------

_pending_logins: dict[str, tuple[str, float, bool]] = {}


def _prune_pending_logins() -> None:
    now = time.time()
    for key, (_, expires, _) in list(_pending_logins.items()):
        if expires <= now:
            del _pending_logins[key]


def start_login(*, browser: bool) -> str:
    """Create state + PKCE pair and return the provider authorize URL."""
    discovery = get_discovery()  # may raise OktaUnavailable
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    _prune_pending_logins()
    if len(_pending_logins) >= MAX_PENDING_LOGINS:
        # Evict oldest; abandoned logins must not exhaust memory.
        oldest = min(_pending_logins, key=lambda k: _pending_logins[k][1])
        del _pending_logins[oldest]
    _pending_logins[state] = (verifier, time.time() + STATE_TTL_SECONDS, browser)

    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    params = urlencode(
        {
            "client_id": os.environ["AGENT_BUS_OKTA_CLIENT_ID"],
            "response_type": "code",
            "scope": scopes(),
            "redirect_uri": redirect_uri(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{discovery['authorization_endpoint']}?{params}"


@dataclass(frozen=True, slots=True)
class LoginResult:
    token_id: str
    raw_token: str
    sub: str
    iss: str
    email: str | None
    name: str | None
    admin: bool
    browser: bool


def complete_login(*, code: str, state: str) -> LoginResult:
    """Exchange the code, read userinfo, mint the Agent Bus token."""
    pending = _pending_logins.pop(state, None)
    if pending is None or pending[1] <= time.time():
        raise OAuthError("Unknown or expired login state; restart the login flow.")
    verifier, _, browser = pending

    discovery = get_discovery()
    try:
        token_response = _client().post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
                "client_id": os.environ["AGENT_BUS_OKTA_CLIENT_ID"],
                "client_secret": os.environ["AGENT_BUS_OKTA_CLIENT_SECRET"],
                "code_verifier": verifier,
            },
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        userinfo_response = _client().get(
            discovery["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo_response.raise_for_status()
    except httpx.HTTPError as e:
        raise OktaUnavailable(f"Login failed: provider error ({e})") from e
    except KeyError as e:
        raise OAuthError(f"Login failed: provider response missing {e}") from e

    userinfo: dict[str, Any] = userinfo_response.json()
    sub = userinfo.get("sub")
    if not sub:
        raise OAuthError("Login failed: userinfo has no sub claim")

    groups = userinfo.get("groups")
    if groups is None:
        logger.warning(
            "Okta userinfo carried no groups claim: token minted without admin. "
            "Configure the groups claim on the Okta app (see docs) or nobody "
            "will ever get admin access."
        )
        groups = []

    admin = admin_group() in groups
    from agent_bus.tokens import get_token_store

    token_id, raw = get_token_store().mint(
        iss=os.environ["AGENT_BUS_OKTA_ISSUER"].rstrip("/"),
        sub=sub,
        email=userinfo.get("email"),
        name=userinfo.get("name"),
        admin=admin,
        browser=browser,
    )
    logger.info("minted token %s sub=%s admin=%s browser=%s", token_id, sub, admin, browser)
    return LoginResult(
        token_id=token_id,
        raw_token=raw,
        sub=sub,
        iss=os.environ["AGENT_BUS_OKTA_ISSUER"].rstrip("/"),
        email=userinfo.get("email"),
        name=userinfo.get("name"),
        admin=admin,
        browser=browser,
    )
