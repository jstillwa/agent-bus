# Plan: Per-user topic access via Okta-minted tokens

## Context
Today a single shared secret (`AGENT_BUS_AUTH_TOKEN`) gates the HTTP server; anyone holding it sees everything. We want per-user tokens minted through an Okta login on this same server, where each user only sees/accesses topics they created, plus a small admin UI gated by the Okta group claim `Permission - agent-bus - Admin`. Greenfield — no production data, no migration constraints.

This plan was grilled to shared understanding (12 decisions) and then adversarially reviewed by Codex (GPT-5.6-sol), Gemini (3.1 Pro), and Kimi K3 (responses in `.project/temp/adversary-uMDJFi/`). Review-driven revisions are folded in below.

## Locked-in decisions

1. **HTTP deployment only.** stdio MCP stays exactly as-is (local DB file, no auth). Documented trust boundary: Okta auth protects *network* callers; host-level actors have the DB file anyway.
2. **Ownership = reserved topic metadata.** `metadata["_owner"] = "<iss>|<sub>"`, stamped by the server at `topic_create`, stripped from client-supplied metadata on **every** metadata write path (invariant + test; `spec.md` documents the reserved `_`-prefix). No Rust core / schema changes. `iss` stored alongside `sub` so a future second IdP can't collide (review finding).
3. **Tokens in a separate SQLite file** (`AGENT_BUS_TOKENS_DB`, default alongside `AGENT_BUS_DB`) via stdlib `sqlite3`, with `PRAGMA user_version` for future migrations. Tables: `tokens(id, token_hash, iss, sub, email, name, admin, browser, created_at, expires_at, revoked_at)`. Opaque token, SHA-256 hash stored; revocation = row update. No `last_used_at` (review: write-per-request bottleneck; not needed by the admin surface). Same-file consolidation was rejected: the core owns that file's schema versioning; instead docs state the two files must be snapshotted together for backup.
4. **Ownership keyed on the Okta user (`iss` + `sub`), not the token.** Multiple tokens per user; all share visibility. MCP tokens: 90-day expiry (`AGENT_BUS_TOKEN_TTL_DAYS`, default 90). Browser cookie tokens: 24h TTL (constant; they are the admin surface). Expired/revoked → 401 + `WWW-Authenticate: Bearer` naming the login URL.
5. **OAuth flow**: Auth Code + PKCE + `state`; env `AGENT_BUS_OKTA_ISSUER`, `AGENT_BUS_OKTA_CLIENT_ID`, `AGENT_BUS_OKTA_CLIENT_SECRET`. Discovery fetched **lazily on first login** and cached (review: Okta unreachable ≠ server won't start; only *missing env vars* fail fast — decision 12). **No JWT verification** — after code exchange, call the provider `userinfo` server-to-server (TLS is the trust anchor). **httpx declared as a direct dependency** (review, unanimous: transitive reliance on the mcp SDK is not a contract).
6. **Token delivery**: show-once page after callback (raw token + copy button + ready MCP client JSON snippet), `Cache-Control: no-store`, no cookie session for regular users.
7. **Shared-secret auth is deleted** (`SharedSecretAuthMiddleware`, `AGENT_BUS_AUTH_TOKEN`, `AGENT_BUS_SECRET`, and the `?token=` query-param path — review: credential leakage into logs/history/Referer). Okta tokens are the only HTTP auth path; auth surfaces: Bearer, X-API-Key, cookie. stdio is the recovery hatch (runbook: `agent-bus cli tokens mint` also works, decision 14).
8. **Non-admin enforcement — foreign topics are invisible, not forbidden** (review-driven revision; replaces `TOPIC_FORBIDDEN`): `topic_join`/`topic_resolve` by foreign id or name → behaves exactly like the topic doesn't exist (not-found error). `topic_create(mode='reuse')` and name resolution resolve **within the caller's owned topics only**; if no owned match, a new same-named topic is created (verified: no UNIQUE constraint on topic names in the core schema). No existence oracle, no name-squatting. List/search/detail on foreign topics → not found / filtered out. Close/rename/delete on foreign topics → not found. Admins see/does everything. Unowned topics: no support path — admin-visible only for deletion.
9. **Admin UI**: standalone `/admin` page (hand-written HTML + vanilla JS, no SPA build) + small JSON endpoints. Browser logins (`?browser=1` or initiated from `/admin`) get the minted token as an HttpOnly cookie.
10. **Admin surface**: list tokens (user, email, created, expires, revoked-at — never raw values), revoke token, list all topics with owner, delete topic. No Okta provisioning, no audit *log storage* — but mints, revocations, deletions, and auth failures are written to the server log (review: zero trace for admin actions is not defensible).
11. **Strict isolation** — zero sharing primitives between users. Future sharing = metadata addition (e.g. `_readers`); not built now.
12. **Fail fast**: `serve` refuses to start without the Okta env vars *and* `AGENT_BUS_PUBLIC_URL` (review: pin the redirect URI from env at startup — deriving from the Host header trusts proxy headers and spreads redirect-URI sprawl). Never default to an open server.
13. **Config**: `AGENT_BUS_PUBLIC_URL` (required, pins redirect URI `<public_url>/auth/callback`); admin group via `AGENT_BUS_ADMIN_GROUP` (default `Permission - agent-bus - Admin`); token format `ab_` + `secrets.token_urlsafe(32)`.
14. **Admin staleness bounded to 24h** (review-driven revision): the admin flag is only honored on **browser-minted cookie tokens (24h TTL)**. Show-once MCP tokens are never admin — admins use the web UI and re-login daily. Okta group removal/offboarding takes effect within 24h; no re-validation machinery needed.
15. **CLI token mint** (review-driven addition): `agent-bus cli tokens mint --sub <sub> [--email --admin]` plus `list`/`revoke` subcommands. Closes the CI/service-account gap (a headless agent otherwise runs as a human's identity) and doubles as the 2am recovery tool. Local-operator trust — same trust level as the stdio hatch.

## Approach

Three new small modules + edits to the two existing surfaces. Identity flows through one place: middleware attaches the authenticated identity (iss, sub, admin, browser, token row id) to `scope["state"]["auth"]`; MCP tools read it via `mcp.get_context().request_context.request` (verified available in the installed mcp SDK); web handlers read it from `request.state`.

**No HTTP request context ⇒ stdio mode ⇒ no enforcement (decision 1).** Explicit transport check: identity missing on an HTTP path fails closed (401), never " unrestricted".

**Guard matrix** (review: enumeration by vibes is how routes get missed). Every row checked against the code before implementation; table in `spec.md`:

| Surface | Non-admin | Admin |
|---|---|---|
| MCP: `topic_create` (new) | stamp `_owner`, strip client `_`-reserved keys | same (owner = admin's sub) |
| MCP: `topic_create` (reuse) | resolve within owned only; else create new same-named | global newest (today's semantics) |
| MCP: `topic_join`, `topic_resolve` (id or name) | owned only; foreign = not-found | all |
| MCP: `sync`, `cursor_reset`, presence, search, close/rename/delete via tools | owned only; foreign = not-found | all |
| MCP: `topic_list`, ping | owned only | all |
| HTTP: `/api/topics*` list/detail/messages/export/search/close/delete | owned only; 404 on foreign | all |
| HTTP: `/api/admin/*`, `/admin` | 403 unless browser-admin token | — |
| HTTP: streams (`/api/stream/*`) | owned topics only; token rechecked each tick (review: revocation must reach live streams) | all |

**Post-filter starvation fix** (review, unanimous): the core applies `LIMIT`/ranking globally, so Python-side filtering after the fact can starve a user (0 results while owned matches exist). Fix within the no-core-change constraint: **internal over-fetch loop** — fetch pages from the core (e.g. 4× the requested limit) and filter until the user's limit is filled or the DB is exhausted. `# ponytail: over-fetch + filter; core WHERE owner=? if scale demands` (upgrade path is a core schema change, deliberately deferred).

### New files
- **`agent_bus/tokens.py`** — stdlib `sqlite3` store (`user_version=1`): `init_db()`, `mint(iss, sub, email, name, admin, browser, ttl) -> (id, raw)`, `lookup(raw) -> row | None` (checks expiry/revocation), `revoke(id)`, `list()`.
- **`agent_bus/oauth.py`** — Okta flow: lazy discovery fetch/cache (validate `issuer` matches configured), `GET /auth/login` (PKCE + state in a bounded in-memory dict: 10-min TTL, max-size eviction, one-time atomic consumption — review findings), `GET /auth/callback` (code exchange → `userinfo` → `sub` + groups → mint → show-once page or HttpOnly cookie + redirect). Warn loudly at mint when the groups claim is absent (review: Okta doesn't emit groups at `userinfo` by default — the whole admin story depends on it; verify against the real tenant in the Okta setup doc). httpx with bounded timeouts; no blind retry on code exchange.
- **`agent_bus/web/admin.html`** — vanilla JS page: token table + revoke buttons, topic table + delete buttons. Rendering via `textContent` only (review: XSS via Okta-sourced emails/topic names). Gated server-side on browser-admin tokens.

### Modified files
- **`agent_bus/auth.py`** — replace `SharedSecretAuthMiddleware` with `TokenAuthMiddleware`: token from Bearer / X-API-Key / cookie (`agent_bus_token`) — **`?token=` removed**; hash-lookup; success ⇒ `scope["state"]["auth"]`; failure ⇒ 401 `WWW-Authenticate: Bearer` + login URL. Exempt `/health`, `/auth/*`, static assets. Cookies: `HttpOnly; SameSite=Strict; Secure` when the public URL is https (localhost http dev exempt — review: otherwise cookies silently fail in the E2E test). Mutating routes additionally check `Origin` (CSRF defense-in-depth).
- **`agent_bus/peer_server.py`** — identity helper (`None` only in stdio mode); ownership guard used at every row of the matrix above; `_owner` stamp/strip; scoped reuse/name resolution (Python pre-resolve among owned topics, bypass core's global reuse for non-admins); `topic_list` filtered.
- **`agent_bus/web/server.py`** — startup env validation (fail fast); lazy Okta init; `/auth/login`, `/auth/callback`, `/admin`; `GET /api/admin/tokens`, `POST /api/admin/tokens/{id}/revoke`; ownership guards per matrix; streams recheck token per tick; remove shared-secret wiring.
- **`agent_bus/cli.py`** — `agent-bus cli tokens mint/list/revoke` (decision 15).
- **`agent_bus/common.py`** — no new error code needed (foreign = `TOPIC_NOT_FOUND`, which exists); keep error plumbing unchanged.
- **`pyproject.toml`** — `httpx>=0.28` added to web extra (decision 5).
- **`tests/`** —
  - `test_tokens.py`: mint/lookup/expiry/revoke; `user_version` present.
  - `test_auth_middleware.py`: Bearer/X-API-Key/cookie paths; `?token=` rejected; exemptions; identity in state; 401 shape; missing HTTP identity fails closed.
  - `test_ownership.py`: create stamps `_owner`; client `_owner` stripped (all metadata write paths); reuse resolves within owned only, creates same-named topic when no owned match; foreign join/resolve/detail = not-found (indistinguishable from missing); list/search over-fetch starvation test (user still gets full page when foreign topics dominate); admin sees all; browser-admin-only admin flag; 24h vs 90d TTLs.
  - `test_oauth.py` (review: highest-risk new code, currently untested): mocked httpx — state mismatch, code-exchange failure, missing groups claim (mint without admin + warning), userinfo failure, discovery cached.
  - One real ASGI end-to-end test: minted token row → real HTTP request through middleware → MCP tool call → ownership enforced (review: injecting identity into scope state directly can miss plumbing regressions).
- **Docs** — `spec.md`: reserved `_`-prefix metadata keys, ownership + visibility semantics per transport (protocol is now deployment-mode-dependent — reviewers flagged this must be explicit), the guard matrix, 24h browser-admin rule. Okta setup guide: Web app, Auth Code + PKCE, pinned redirect URI, **groups claim at `userinfo`** with tenant-verification step, group name, plus the offboarding runbook (revoke via CLI/admin) and stdio/CLI recovery runbook. Docs-site update per the agent-bus-docs-site skill.

### Reuse
- `agent_bus/common.py` `tool_error` — `TOPIC_NOT_FOUND` already exists; no new error code.
- `agent_bus/db.py` `topic_list`/`topic_list_with_counts`/`topic_get_with_counts` — already return `metadata`; owner filtering stays Python-side.
- Existing `/api/topics*` handlers — admin UI reuses them unfiltered; only the guard differs.
- httpx (now declared directly) for the OAuth/token endpoints.
- Cookie parsing logic from the old middleware — port, don't reinvent.

## Steps
- [x] 1. `pyproject.toml`: declare httpx; `agent_bus/tokens.py` + `tests/test_tokens.py`
- [x] 2. Rewrite `agent_bus/auth.py` as `TokenAuthMiddleware` + `tests/test_auth_middleware.py`
- [x] 3. Ownership guard + wire into `peer_server.py` tools per guard matrix (stamp/strip `_owner`, scoped reuse/name resolution, over-fetch list filter) + `tests/test_ownership.py`
- [x] 4. Same guards in `web/server.py` API handlers + stream token recheck
- [x] 5. `agent_bus/oauth.py`: login/callback/show-once + lazy discovery + fail-fast env validation + `tests/test_oauth.py`
- [x] 6. Admin: `/admin` page + `/api/admin/tokens` list + revoke (browser-admin gated, CSRF/cookie hardening)
- [x] 7. CLI: `agent-bus cli tokens mint/list/revoke`
- [x] 8. Delete shared-secret code paths and env handling; grep for stragglers
- [x] 9. ASGI end-to-end ownership test
- [x] 10. Docs: `spec.md` (semantics, reserved keys, matrix, transport bifurcation), Okta setup guide + runbooks, docs-site update
- [x] 11. `uv run ruff format && uv run ruff check && uv run ty check && uv run pytest`

## Verification
- **Automated**: pytest suite above (hermetic, no Okta; oauth mocked).
- **Manual E2E with a real Okta app** (requires an Okta dev tenant — **verify the groups-at-userinfo claim works before starting step 5**, per review):
  1. Create Okta "Web" app: Auth Code + PKCE, redirect URI `<AGENT_BUS_PUBLIC_URL>/auth/callback` (exact match), groups claim configured at `userinfo` (`Permission - agent-bus - Admin` for the admin test user).
  2. Missing Okta env or `AGENT_BUS_PUBLIC_URL` → `serve` refuses with message; Okta unreachable (valid envs) → server starts, `/auth/login` returns 503, existing tokens keep working.
  3. Non-admin login → token shown once → MCP client (or curl `Authorization: Bearer ab_...`) creates topics, sees only own topics in `topic_list` and the web UI; a second user's token gets not-found for the first user's topic id/name (indistinguishable from a missing topic).
  4. `mode='reuse'` with a foreign same-named topic → creates a new own topic with that name, no error, no handover.
  5. List starvation: seed 100 foreign topics, 5 owned; user's list page still returns 5.
  6. Admin-group login → `/admin` works: lists tokens + all topics; revoke kills the other token's access immediately (including live streams); topic delete works. Admin cookie expires in 24h; MCP-minted token (admin-group user) gets 403 on `/api/admin/*`.
  7. Tamper: client passes `metadata={"_owner": "<someone else>"}` → stripped, server value wins.
  8. `AGENT_BUS_TOKEN_TTL_DAYS=0` → minted token immediately 401; 401 body names the login URL.
  9. Offboarding runbook: revoke via `agent-bus cli tokens revoke` while server is down (2am path).
- **Gates**: ruff format/check, ty check, pytest — per AGENTS.md.

## Review disposition summary
Unanimous/consensus findings folded in: over-fetch starvation fix, bounded OAuth state, `?token=` removal, direct httpx dep, CSRF/cookie/show-once hardening, groups-claim tenant verification + loud warning, `iss`+`sub` keying, pinned redirect URI, lazy discovery, guard matrix, server-log audit lines, `last_used_at` dropped, stream revocation recheck, `user_version` on tokens DB, backup-coupling doc note. Rejected (conscious, documented): first-class owner column in the Rust core and MCP Protected Resource Metadata compliance (deferred — metadata + over-fetch has a marked upgrade path; `WWW-Authenticate: Bearer` is included as the cheap part). `RETHINK` verdicts from Codex/Gemini rest primarily on the pagination/search concern — addressed via over-fetch + the ponytail-marked upgrade path — and on standards-level OAuth, consciously deferred for this internal-tool scope.
