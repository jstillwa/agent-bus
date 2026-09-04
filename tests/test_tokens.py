from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from agent_bus.tokens import (
    TOKEN_PREFIX,
    TOKENS_SCHEMA_VERSION,
    TokenStore,
    token_ttl_seconds,
)


def make_store(tmp_path: Path) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.sqlite")


def test_mint_and_lookup(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    token_id, raw = store.mint(iss="https://okta.example", sub="user-123", email="a@b.c")
    assert raw.startswith(TOKEN_PREFIX)

    row = store.lookup(raw)
    assert row is not None
    assert row["id"] == token_id
    assert row["sub"] == "user-123"
    assert row["iss"] == "https://okta.example"
    assert row["admin"] == 0
    assert row["browser"] == 0
    # raw token never stored, only its hash
    all_rows = store.list_tokens()
    assert len(all_rows) == 1
    assert "ab_" not in str(all_rows)


def test_lookup_unknown_token(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.lookup("ab_nope") is None


def test_revoke(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    token_id, raw = store.mint(iss="iss", sub="s")
    assert store.revoke(token_id) is True
    assert store.lookup(raw) is None
    # idempotent: second revoke reports nothing changed
    assert store.revoke(token_id) is False


def test_expiry(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _, raw = store.mint(iss="iss", sub="s", ttl_seconds=0)
    assert store.lookup(raw) is None  # already expired


def test_browser_tokens_get_24h_ttl(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _, raw_mcp = store.mint(iss="iss", sub="s")
    _, raw_browser = store.mint(iss="iss", sub="s", browser=True)
    now = time.time()
    mcp_row = store.lookup(raw_mcp)
    browser_row = store.lookup(raw_browser)
    assert mcp_row is not None and browser_row is not None
    assert mcp_row["expires_at"] - now > 89 * 86400
    assert 23 * 3600 < browser_row["expires_at"] - now <= 24 * 3600


def test_user_version_set(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    conn = sqlite3.connect(store.path)
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == TOKENS_SCHEMA_VERSION


def test_ttl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BUS_TOKEN_TTL_DAYS", "7")
    assert token_ttl_seconds() == 7 * 86400
    monkeypatch.setenv("AGENT_BUS_TOKEN_TTL_DAYS", "x")
    with pytest.raises(ValueError, match="TTL"):
        token_ttl_seconds()


# --- CLI commands -------------------------------------------------------------


def test_cli_tokens_mint_list_revoke(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from agent_bus.cli import cli

    store_path = tmp_path / "cli-tokens.sqlite"
    monkeypatch.setenv("AGENT_BUS_TOKENS_DB", str(store_path))

    runner = CliRunner()
    minted = runner.invoke(cli, ["tokens", "mint", "--sub", "ci-bot", "--iss", "cli"])
    assert minted.exit_code == 0, minted.output
    raw = (
        next(ln for ln in minted.output.splitlines() if "Bearer" in ln).split("Bearer ")[1].strip()
    )

    listed = runner.invoke(cli, ["tokens", "list"])
    assert listed.exit_code == 0
    assert "ci-bot" in listed.output
    assert raw[:8] not in listed.output  # raw tokens never listed

    token_id = next(ln for ln in minted.output.splitlines() if ln.startswith("Token ID")).split(
        ": "
    )[1]
    revoked = runner.invoke(cli, ["tokens", "revoke", token_id])
    assert revoked.exit_code == 0
    assert TokenStore(path=store_path).lookup(raw) is None


def test_cli_admin_note_without_browser(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from agent_bus.cli import cli

    monkeypatch.setenv("AGENT_BUS_TOKENS_DB", str(tmp_path / "t.sqlite"))
    result = CliRunner().invoke(cli, ["tokens", "mint", "--sub", "ops", "--admin"])
    assert result.exit_code == 0
    assert "only honored on --browser tokens" in result.output
