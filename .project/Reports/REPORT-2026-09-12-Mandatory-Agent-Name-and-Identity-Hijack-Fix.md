# REPORT: Mandatory Agent Name, Identity Hijack Resolution & Reclaim Token Leak Fix

## Request Summary

| Field | Value |
| --- | --- |
| Date Requested | 2026-09-12 |
| Date Completed | 2026-09-12 |
| Status | Resolved |
| Systems Touched | `jstillwa/agent-bus`, Railway Production (`agent-bus-production-f162.up.railway.app`), GitHub PR #8 |

## Request Details

An agent running in an interactive session on topic `1fbb043e02` was unable to see new messages posted by a peer agent (`pi-coordinator`), appearing "blind" until a background watcher woke up.

Investigation of server logs, database timestamps, and server code revealed that the issue was an ambient identity hijacking flaw:
1. When callers omitted `agent_name` on `sync()` or `cursor_reset()`, `peer_server.py` fell back to an in-memory process-wide dictionary `_joined_identities[topic_id]`.
2. Whichever peer called `topic_join()` last clobbered this mapping for the entire topic across all HTTP clients worldwide.
3. The interactive agent's sync calls were therefore resolved as its peer (`pi-coordinator`), which caused `include_self=False` to actively filter out the peer's messages as self-authored.
4. An advisory RFC panel was convened on Agent Bus topic `f7c20e5b88` with 5 model families (kimi, gemini, astra, deepseek, qwen) to analyze the architecture and debate solutions.
5. The consensus fix (Option A) was implemented, tested, PR'd, verified green in CI across all platforms, merged into `main`, deployed to Railway production as v0.6.0, and verified against the live endpoint.

---

## Implementation

### 1. Purged Insecure In-Memory Identity Layer (`agent_bus/peer_server.py`)
- Completely removed `_joined_identities: dict[str, JoinedIdentity]`, `JoinedIdentity`, `_session_key()`, and `_agent_name_for_topic()`.
- FastMCP session objects (`id(ctx.session)`) were unsafe due to CPython address recycling across GC, and HTTP proxies strip client-provided metadata.
- Made `agent_name: str` strictly **mandatory** on both `sync()` and `cursor_reset()`. Omission now fails immediately at schema validation.

### 2. Closed `reclaim_token` Disclosure Vulnerability (`agent_bus/peer_server.py`)
- Removed the cached-identity fast-path in `topic_join` (`existing is not None and existing.agent_name == normalized`).
- Previously, an unauthorized session attempting to re-claim an existing name with an invalid token would be handed the victim's valid token from memory.
- All joins now unconditionally route to `db.reserve_agent_name`, which checks the token against SQLite and rejects invalid claims with `AGENT_NAME_IN_USE` without token disclosure.

### 3. MCP-Boundary Membership Assertion (`src/lib.rs`, `agent_bus/db.py`, `agent_bus/peer_server.py`)
- Added `is_joined` query in Rust `CoreDb` checking `EXISTS(agent_name_reservations) UNION EXISTS(cursors)`.
- Wrapped in `AgentBusDB.is_joined()`.
- Enforced at the MCP tool entrypoint in `peer_server.py` for `sync` and `cursor_reset`: callers using arbitrary unjoined names receive structured `AGENT_NOT_JOINED`.
- Placed strictly at the MCP boundary so internal database calls and the Web API (`POST /api/topics/{id}/messages` with default unreserved sender `"operator"`) remain unblocked.

### 4. CI & Test Suite Repairs
- Formatted `src/lib.rs` with `cargo fmt`.
- Fixed 3 `cargo clippy` warnings in `src/lib.rs` (too-many-arguments on `poll_create`, type-complexity on `poll_get`, and `sort_by_key` replacement).
- Fixed Playwright e2e test configuration in `frontend/playwright.config.ts`: updated readiness probe to `http://127.0.0.1:4173/health` instead of `/` (which 307-redirected to unconfigured Okta and returned 503).
- Cleaned deprecation warnings in test fixtures (`streamable_http_client` and Starlette cookie setters).

### 5. Durable Regression Tests
- Added `test_sync_and_cursor_reset_require_agent_name` in `tests/test_validation.py`.
- Added `test_topic_join_does_not_leak_reclaim_token` in `tests/test_validation.py`.
- Added `test_regression_rfc.py` validating cross-session token leak rejection and phantom name rejection via stdio client sessions.

### 6. Version Bump & Documentation
- Bumped `pyproject.toml` and `Cargo.toml` to `0.6.0` (breaking API change under SemVer).
- Documented changes in `CHANGELOG.md`.
- Updated `spec.md` and `site/content/docs/reference/implementation-spec.mdx` (§2.3, §4.3, §4.4).

---

## Verification

| Check | Result |
| --- | --- |
| Schema validation rejects omitted `agent_name` on `sync()` | Confirmed |
| Schema validation rejects omitted `agent_name` on `cursor_reset()` | Confirmed |
| Cross-session duplicate join fails `AGENT_NAME_IN_USE` without leaking token | Confirmed |
| MCP `sync()` with unjoined phantom name returns `AGENT_NOT_JOINED` | Confirmed |
| Web UI API and internal `db.sync_once` paths remain unblocked | Confirmed |
| Local Python test suite (173 tests) | 173 passed, 0 failures, 0 warnings |
| Rust toolchain (`cargo clippy -D warnings`, `cargo fmt --check`) | Passed |
| Frontend unit tests & Playwright e2e smoke tests | Passed |
| GitHub Actions CI matrix (Ubuntu, macOS, Windows, Wheels, Site, Frontend) | All 9 jobs green (Run 34703191354) |
| Railway production deployment (`19720346-7551-4a79-91d1-0380fb246d04`) | Successful (Status: Online) |
| Live production `/health` endpoint | `{"status":"ok","version":"0.6.0"}` |
| Live production end-to-end multi-agent test | Confirmed (Alice/Bob ping-pong, token leak protection, phantom rejection) |

---

## Commands Executed

```bash
# Code checks and unit testing
cargo fmt --check
cargo clippy --all-targets -- -D warnings
uv run ty check
.venv/bin/ruff check .
.venv/bin/pytest -q -p no:cacheprovider

# Frontend tests
cd frontend && pnpm test && pnpm test:e2e

# Git and GitHub PR lifecycle
git checkout -b fix/mandatory-agent-name
git push -u origin fix/mandatory-agent-name
gh pr create --title "fix(core): close ambient identity hijack, phantom cursors, and token leak" ...
gh workflow run ci.yml --ref fix/mandatory-agent-name
gh pr merge 8 --merge

# Railway deployment and verification
railway up -s d1360e98-5bd6-464c-92df-b37699bea567 -d
curl -s https://agent-bus-production-f162.up.railway.app/health
```

---

## Resource Usage

| Model | Input Tokens | Output Tokens | Cache Read Tokens | Est. Cost |
| :--- | :--- | :--- | :--- | :--- |
| `gemini/gemini-flash-latest` | 6,477,924 | 107,345 | 102,481,983 (94% cached) | $8.34 |

---

## Outstanding Items

- None. Fix is merged, verified in CI, and running live in production.

## Related Documentation

- PR #8: https://github.com/jstillwa/agent-bus/pull/8
- Spec: `spec.md` (§2.3, §4.3, §4.4)
- Changelog: `CHANGELOG.md` ([0.6.0])
