# How to serve Agent Bus behind Okta auth

The HTTP deployment (`agent-bus serve`) is multi-user: every user gets their own token by
logging in with your Okta (OIDC) account, and each token only sees the topics its user created.
Users in a specific Okta group get a small admin page for tokens and topic cleanup.

## Configure the Okta app

Create an **OIDC Web app** in Okta (Admin → Applications → Create App Integration →
OIDC - OpenID Connect → Web). Set:

- **Sign-in redirect URI**: `<your-server>/auth/callback` — exactly matching
  `AGENT_BUS_PUBLIC_URL` (e.g. `https://bus.example.com/auth/callback`).
- **Grant type**: Authorization Code + PKCE (S256).
- **Sign-out**: not used.

Then:

1. Assign users/groups to the app.
2. Make the **groups claim** available at the `userinfo` endpoint: create a groups claim
   (`docs: groups → claim "groups"`) on the app's ID token **and** userinfo, or on your
   authorization server's token/userinfo. The server reads `groups` from the `userinfo`
   response; Okta does **not** emit it by default. A missing claim is logged loudly at mint
   time — without it nobody can ever mint an admin token.
3. Remember the admin group name; the server expects the group `Permission - agent-bus - Admin`
   by default (override with `AGENT_BUS_ADMIN_GROUP`).

## Configure the server

```bash
export AGENT_BUS_OKTA_ISSUER="https://<your-okta-domain>/oauth2/<server-id>"
export AGENT_BUS_OKTA_CLIENT_ID="<client id>"
export AGENT_BUS_OKTA_CLIENT_SECRET="<client secret>"
export AGENT_BUS_PUBLIC_URL="https://bus.example.com"
uvx --from "agent-bus-mcp[web]" agent-bus serve
```

`agent-bus serve` **refuses to start** without all four variables — it will never silently serve
an unauthenticated bus. If Okta is unreachable at runtime, only the login flow returns 503;
existing tokens keep working.

## The user flow

1. User visits `https://<your-server>/auth/login`, logs in to Okta.
2. The page shows their token **once**, with a ready MCP client config snippet
   (`Authorization: Bearer ab_…`). Tokens expire after 90 days; log in again to mint a new one.
3. Agents/CLI use that token via `Authorization: Bearer` (or `X-API-Key`) against
   `https://<your-server>/mcp`. They only see topics that user created. Foreign topics behave as
   if they did not exist.

There is no cross-user sharing in this version. Topic names are not globally unique: two users
can each have a topic called `code-review` and never collide.

## Admin

Members of the admin group log in via `/auth/login?browser=1`: they get a 24h browser session
and can open `/admin` to:

- list tokens (user, expiry, revoked state — raw tokens are never shown again) and revoke them,
- list all topics (with owner) and delete unowned leftovers.

Admin authority expires after 24 hours — a user removed from the Okta admin group (or
offboarded) loses admin within a day. Log in again to refresh.

## Runbooks

**Offboard a user**: revoke their tokens in `/admin`, or from the server host while the server
is even down:

```bash
uv run agent-bus cli tokens list
uv run agent-bus cli tokens revoke <token-id>
```

Okta-side deactivation stops new logins but does not revoke already-minted tokens — revoke them.

**2am / Okta is broken and you need a token now** (server host access):

```bash
uv run agent-bus cli tokens mint --sub ci-bot --email ops@example.com
```

CLI minting uses the same trust as the stdio transport: whoever can run commands on the server
host already owns the SQLite file. This is also the path for CI/headless agents, so they don't
run under a human's identity.

**Where tokens live**: a separate SQLite file next to the main DB (`tokens.sqlite`, override with
`AGENT_BUS_TOKENS_DB`). Back it up together with the main DB.
