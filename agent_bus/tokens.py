"""Per-user API token store for the Agent Bus HTTP deployment.

Tokens are opaque random strings (`ab_` + urlsafe base64). Only the SHA-256
hash is stored; the raw token is shown exactly once at mint time (show-once
page or CLI output). Lives in its own SQLite file so the Rust core keeps
sole ownership of the main DB's schema.

Backup note: this file and the main Agent Bus DB must be snapshotted together.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

TOKEN_PREFIX = "ab_"
TOKENS_SCHEMA_VERSION = 1
DEFAULT_TOKEN_TTL_DAYS = 90
# Browser-minted tokens are the admin surface; the short TTL bounds how long
# a stale Okta admin-group membership can act as admin (decision: 24h).
BROWSER_TOKEN_TTL_SECONDS = 24 * 60 * 60

_DEFAULT_DIR = Path.home() / ".agent_bus"
_DEFAULT_DB = _DEFAULT_DIR / "agent_bus.sqlite"


def _default_tokens_db_path() -> Path:
    main_db = Path(os.environ.get("AGENT_BUS_DB") or _DEFAULT_DB)
    return main_db.parent / "tokens.sqlite"


def token_ttl_seconds() -> int:
    raw = os.environ.get("AGENT_BUS_TOKEN_TTL_DAYS")
    if raw is None or raw == "":
        return DEFAULT_TOKEN_TTL_DAYS * 86400
    try:
        days = int(raw)
    except ValueError as e:
        raise ValueError("AGENT_BUS_TOKEN_TTL_DAYS must be an int") from e
    return days * 86400


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, *, path: str | Path | None = None) -> None:
        if path is None:
            path = os.environ.get("AGENT_BUS_TOKENS_DB") or _default_tokens_db_path()
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > TOKENS_SCHEMA_VERSION:
            raise RuntimeError(
                f"tokens DB schema v{version} is newer than supported "
                f"v{TOKENS_SCHEMA_VERSION}; upgrade agent-bus."
            )
        if version < TOKENS_SCHEMA_VERSION:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    iss TEXT NOT NULL,
                    sub TEXT NOT NULL,
                    email TEXT,
                    name TEXT,
                    admin INTEGER NOT NULL DEFAULT 0,
                    browser INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_sub ON tokens(sub);
                """
            )
            self._conn.execute(f"PRAGMA user_version={TOKENS_SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def mint(
        self,
        *,
        iss: str,
        sub: str,
        email: str | None = None,
        name: str | None = None,
        admin: bool = False,
        browser: bool = False,
        ttl_seconds: int | None = None,
    ) -> tuple[str, str]:
        """Create a token. Returns (token_id, raw_token); raw is never stored."""
        token_id = f"tok_{secrets.token_hex(8)}"
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        created = time.time()
        ttl = (
            BROWSER_TOKEN_TTL_SECONDS
            if browser
            else (token_ttl_seconds() if ttl_seconds is None else ttl_seconds)
        )
        self._conn.execute(
            """
            INSERT INTO tokens
                (id, token_hash, iss, sub, email, name, admin, browser, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                _hash_token(raw),
                iss,
                sub,
                email,
                name,
                int(admin),
                int(browser),
                created,
                created + ttl,
            ),
        )
        self._conn.commit()
        return token_id, raw

    def lookup(self, raw: str) -> dict[str, Any] | None:
        """Return the token row if valid (not expired, not revoked), else None."""
        row = self._conn.execute(
            "SELECT * FROM tokens WHERE token_hash = ?", (_hash_token(raw),)
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None or row["expires_at"] <= time.time():
            return None
        return dict(row)

    def valid(self, token_id: str) -> bool:
        """Cheap revocation/expiry check by id (used by long-lived streams)."""
        row = self._conn.execute(
            "SELECT expires_at, revoked_at FROM tokens WHERE id = ?", (token_id,)
        ).fetchone()
        if row is None:
            return False
        return row["revoked_at"] is None and row["expires_at"] > time.time()

    def revoke(self, token_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), token_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_tokens(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM tokens ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


_store: TokenStore | None = None


def get_token_store() -> TokenStore:
    """Module-level store for the running server process (lazy, env-configured)."""
    global _store
    if _store is None:
        _store = TokenStore()
    return _store


def set_token_store(store: TokenStore | None) -> None:
    """Override the process store (tests, CLI)."""
    global _store
    _store = store
