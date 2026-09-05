"""FastAPI web server for the Agent Bus SPA and browser APIs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from starlette.routing import Route

from agent_bus.auth import AuthIdentity, TokenAuthMiddleware, identity_from_scope
from agent_bus.db import AgentBusDB, DBBusyError, TopicNotFoundError
from agent_bus.models import Cursor, Message
from agent_bus.ownership import OWNER_KEY, is_visible, owner_key

STATIC_DIR = Path(__file__).parent / "static"
SPA_INDEX = STATIC_DIR / "index.html"
DEFAULT_PAGE_SIZE = 50
TOPICS_STREAM_INTERVAL_SECONDS = 2.0
TOPIC_STREAM_INTERVAL_SECONDS = 2.0
STREAM_HEARTBEAT_SECONDS = 15.0
PRESENCE_WINDOW_SECONDS = 300
SERVER_SHUTDOWN_GRACE_SECONDS = 2

SearchMode = Literal["fts", "semantic", "hybrid"]
TopicStatusFilter = Literal["open", "closed", "all"]
TopicSort = Literal["last_updated_desc", "created_desc", "created_asc"]


class PostMessageRequest(BaseModel):
    content_markdown: str = Field(..., min_length=1, max_length=65536)
    sender: str = Field(default="operator", min_length=1, max_length=64)
    message_type: str = Field(default="message", min_length=1, max_length=32)
    reply_to: str | None = Field(default=None)


class CloseTopicRequest(BaseModel):
    reason: str | None = Field(default="closed via web UI")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Run the MCP streamable-HTTP session manager for the lifetime of the server.
    from agent_bus.peer_server import mcp as mcp_server

    mcp_server.streamable_http_app()  # ensure lazy session manager is created
    session_manager = mcp_server.session_manager
    if getattr(session_manager, "_has_started", False):
        # Session managers are single-run; tolerate repeated lifespans (e.g. tests).
        yield
        return
    async with session_manager.run():
        yield


app = FastAPI(title="Agent Bus MCP", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(TokenAuthMiddleware)  # ty: ignore[invalid-argument-type]  # starlette's _MiddlewareFactory ParamSpec protocol; class shape is correct (pre-existing with SharedSecretAuthMiddleware)

_db: AgentBusDB | None = None


# --- auth helpers -----------------------------------------------------------


def request_auth(request: Request) -> AuthIdentity | None:
    return identity_from_scope(request.scope)


def guard_topic(request: Request, topic_id: str) -> None:
    """404 when the caller cannot see the topic (foreign = invisible). Admins see all.

    Fails closed: no identity (only reachable if middleware was bypassed) → 404.
    """
    auth = request_auth(request)
    if auth is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    if auth.admin:
        return
    try:
        topic = get_db().get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        raise HTTPException(status_code=404, detail="Topic not found") from None
    if not is_visible(auth, topic):
        raise HTTPException(status_code=404, detail="Topic not found")


def row_is_visible(auth: AuthIdentity, row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") or {}
    return metadata.get(OWNER_KEY) == owner_key(auth)


def token_still_valid(request: Request) -> bool:
    """Recheck the caller's token (revocation/expiry) for long-lived streams."""
    auth = request_auth(request)
    if auth is None:
        return False
    from agent_bus.tokens import get_token_store

    return get_token_store().valid(auth.token_id)


@app.get("/health")
async def health() -> dict[str, Any]:
    from agent_bus.version import __version__

    return {"status": "ok", "version": __version__}


# --- auth: Okta login flow --------------------------------------------------


ADMIN_PAGE = Path(__file__).parent / "admin.html"


def require_browser_admin(request: Request) -> None:
    """Admin routes require an admin-group *browser* token (24h cookie session).

    MCP/show-once tokens are never admin: the flag is only honored on
    browser-minted tokens so a stale Okta group membership is bounded to 24h.
    """
    auth = request_auth(request)
    if auth is None or not auth.admin or not auth.browser:
        raise HTTPException(
            status_code=403,
            detail=(
                "Admin access requires a browser session with the admin group claim. "
                "Log in via /auth/login?browser=1."
            ),
        )


def check_cookie_csrf(request: Request) -> None:
    """Origin check for mutations authenticated via cookie (CSRF defense).

    Bearer/API-key requests are CSRF-immune and skip this. Cookie-authed
    mutations must carry a same-origin Origin header (browsers always send
    one for cross-origin and most same-origin POSTs).
    """
    auth = request_auth(request)
    if auth is None or not auth.browser:
        return
    origin = request.headers.get("origin")
    if origin is None:
        raise HTTPException(status_code=403, detail="Missing Origin header")
    from agent_bus.oauth import public_url

    if origin.rstrip("/") != public_url():
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> Response:
    """Admin page. No token -> redirect to the browser login; token without the
    admin group (or not a browser session) -> HTML 403. Data endpoints in
    /api/admin/* remain token-gated regardless."""
    auth = request_auth(request)
    if auth is None:
        return RedirectResponse("/auth/login?browser=1")
    if not (auth.admin and auth.browser):
        return HTMLResponse(
            """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Agent Bus — Admin</title></head>
<body style="font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem">
<h1>403 — not an admin session</h1>
<p>This page needs a browser login (24h session) by an account in the
<code>Permission - agent-bus - Admin</code> Okta group. MCP tokens are never admin.</p>
<p><a href="/auth/login?browser=1">Log in again</a></p>
</body></html>""",
            status_code=403,
        )
    if not ADMIN_PAGE.is_file():
        raise HTTPException(status_code=500, detail="admin.html missing")
    return FileResponse(ADMIN_PAGE)


@app.get("/api/admin/tokens")
async def api_admin_tokens(request: Request) -> dict[str, Any]:
    require_browser_admin(request)
    from agent_bus.tokens import get_token_store

    tokens = [
        {
            "id": row["id"],
            "iss": row["iss"],
            "sub": row["sub"],
            "email": row["email"],
            "name": row["name"],
            "admin": bool(row["admin"]),
            "browser": bool(row["browser"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
        }
        for row in get_token_store().list_tokens()  # raw token hashes never leave the server
    ]
    return {"tokens": tokens}


@app.post("/api/admin/tokens/{token_id}/revoke")
async def api_admin_revoke(request: Request, token_id: str) -> dict[str, Any]:
    require_browser_admin(request)
    check_cookie_csrf(request)
    from agent_bus.tokens import get_token_store

    revoked = get_token_store().revoke(token_id)
    auth = request_auth(request)
    import logging

    logging.getLogger("agent_bus").info(
        "admin revoke token=%s by=%s", token_id, auth.sub if auth else "?"
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    return {"status": "ok", "token_id": token_id, "revoked": True}


@app.get("/auth/login")
@app.get("/auth/login")
async def auth_login(browser: bool = False) -> Response:
    """Redirect to the Okta authorize URL. `?browser=1` mints a cookie token (admin UI)."""
    from agent_bus import oauth

    try:
        url = oauth.start_login(browser=browser)
    except oauth.OktaUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from None
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(code: str = "", state: str = "") -> Response:
    from agent_bus import oauth

    if not code or not state:
        return HTMLResponse(
            "<h1>Login failed</h1><p>Missing code/state from the provider.</p>", 400
        )
    try:
        result = oauth.complete_login(code=code, state=state)
    except oauth.OAuthError as e:
        return HTMLResponse(f"<h1>Login failed</h1><p>{e}</p>", 400)

    if result.browser:
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            "agent_bus_token",
            result.raw_token,
            max_age=24 * 60 * 60,
            path="/",
            httponly=True,
            samesite="strict",
            secure=oauth.public_url().startswith("https://"),
        )
        return response

    # Show-once page: the raw token is displayed exactly this once.
    snippet = (
        '{\n  "mcpServers": {\n    "agent-bus": {\n'
        f'      "url": "{oauth.public_url()}/mcp",\n'
        '      "headers": { "Authorization": "Bearer YOUR_TOKEN" }\n'
        "    }\n  }\n}"
    )
    # Group members: point them at the browser admin login (MCP tokens are
    # never admin — the admin flag is only honored on browser sessions).
    admin_link = (
        "<h2>Admin</h2>"
        "<p>Your account is in the admin group. "
        '<a href="/auth/login?browser=1">Open the admin interface</a> '
        "(logs in again and mints a 24h admin browser session).</p>"
        if result.admin
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Agent Bus token</title>
<style>body{{font-family:system-ui,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1rem}}
code{{background:#f1e8da;padding:.15rem .4rem;border-radius:.3rem;word-break:break-all}}
pre{{background:#211b16;color:#f6f1e9;padding:1rem;border-radius:.5rem;overflow:auto}}</style></head>
<body>
<h1>Your Agent Bus token</h1>
<p>Copy it now — it is never shown again:</p>
<p><code id="tok">{result.raw_token}</code></p>
<p><button onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent)">Copy token</button></p>
<h2>MCP client config</h2>
<pre>{snippet}</pre>
<p>Replace <code>YOUR_TOKEN</code> with the token above. Expires in 90 days; log in again to mint a new one.</p>
{admin_link}
</body></html>""",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def setup_mcp_routes() -> None:
    """Mount MCP streamable-HTTP (/mcp) and SSE (/sse, /messages) transports.

    Routes are inserted ahead of the SPA catch-all so they win matching.
    GET /mcp is answered 405 (spec-allowed): the standalone server->client SSE
    stream is unused here and some HTTP/1.1 clients stall subsequent POSTs while
    an unread streaming response body is open on the same origin.
    """
    from agent_bus.peer_server import mcp as mcp_server

    mcp_app = mcp_server.streamable_http_app()
    sse_app = mcp_server.sse_app()

    class NoStandaloneStreamApp:
        """ASGI wrapper; must be a callable object so Starlette Route keeps it as raw ASGI."""

        def __init__(self, asgi_app: Any) -> None:
            self._app = asgi_app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http" and scope.get("method") == "GET":
                await PlainTextResponse("Method Not Allowed", status_code=405)(scope, receive, send)
                return
            await self._app(scope, receive, send)

    mcp_no_standalone_stream = NoStandaloneStreamApp(mcp_app)

    routes = [
        Route("/mcp", mcp_no_standalone_stream, methods=["GET", "POST", "DELETE", "OPTIONS"]),
        *reversed(sse_app.routes),
    ]
    for route in reversed(routes):
        app.router.routes.insert(0, route)


class SSEStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            return


class ImmediateSigintServer:
    def __init__(self, config) -> None:
        import uvicorn

        class _Server(uvicorn.Server):
            def handle_exit(self, sig: int, frame) -> None:
                super().handle_exit(sig, frame)
                if sig == signal.SIGINT:
                    self.force_exit = True

        self._server = _Server(config=config)

    def run(self) -> None:
        self._server.run()


def get_db() -> AgentBusDB:
    if _db is None:
        raise RuntimeError("Database not initialized")
    return _db


def init_db(db_path: str | None = None) -> None:
    global _db
    _db = AgentBusDB(path=db_path)


def now() -> float:
    return time.time()


def format_missing_bundle_response() -> Response:
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <title>Agent Bus MCP Web UI</title>
            <style>
              body {
                margin: 0;
                min-height: 100vh;
                display: grid;
                place-items: center;
                background: #f6f1e9;
                color: #211b16;
                font-family: "Geist Variable", system-ui, sans-serif;
              }
              main {
                max-width: 42rem;
                padding: 2rem;
                border-radius: 1rem;
                background: white;
                box-shadow: 0 20px 60px rgba(33, 27, 22, 0.12);
              }
              code {
                background: #f1e8da;
                border-radius: 0.375rem;
                padding: 0.15rem 0.4rem;
              }
            </style>
          </head>
          <body>
            <main>
              <h1>Frontend bundle not found</h1>
              <p>
                This checkout does not have built web assets yet. Run
                <code>pnpm --dir frontend install</code> and
                <code>pnpm --dir frontend build</code>, then restart
                <code>agent-bus serve</code>.
              </p>
            </main>
          </body>
        </html>
        """,
        status_code=503,
    )


def spa_index_response() -> Response:
    if not SPA_INDEX.exists():
        return format_missing_bundle_response()
    return FileResponse(SPA_INDEX)


def serialize_cursor(cursor: Cursor) -> dict[str, Any]:
    return {
        "topic_id": cursor.topic_id,
        "agent_name": cursor.agent_name,
        "last_seq": cursor.last_seq,
        "updated_at": cursor.updated_at,
    }


def serialize_message(message: Message, sender_by_msg_id: dict[str, str]) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "topic_id": message.topic_id,
        "seq": message.seq,
        "sender": message.sender,
        "message_type": message.message_type,
        "reply_to": message.reply_to,
        "reply_to_sender": sender_by_msg_id.get(message.reply_to) if message.reply_to else None,
        "content_markdown": message.content_markdown,
        "metadata": message.metadata,
        "client_message_id": message.client_message_id,
        "created_at": message.created_at,
    }


def normalize_topic_summary(row: dict[str, Any]) -> dict[str, Any]:
    counts = cast(dict[str, int], row["counts"])
    return {
        "topic_id": row["topic_id"],
        "name": row["name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "close_reason": row["close_reason"],
        "metadata": row["metadata"],
        "message_count": counts["messages"],
        "last_seq": counts["last_seq"],
        "last_message_at": row.get("last_message_at"),
        "last_updated_at": row.get("last_updated_at", row["created_at"]),
    }


def list_topic_summaries(
    db: AgentBusDB,
    *,
    status: TopicStatusFilter,
    sort: TopicSort,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = db.topic_list_with_counts(status=status, sort=sort, query=query, limit=limit)
    return [normalize_topic_summary(row) for row in rows]


def get_topic_summary(db: AgentBusDB, *, topic_id: str) -> dict[str, Any]:
    return normalize_topic_summary(db.topic_get_with_counts(topic_id=topic_id))


def serialize_topic_messages(
    db: AgentBusDB,
    messages: list[Message],
) -> tuple[list[dict[str, Any]], int | None, int | None]:
    reply_to_ids = [message.reply_to for message in messages if message.reply_to]
    sender_lookup = db.get_senders_by_message_ids(reply_to_ids) if reply_to_ids else {}
    payload = [serialize_message(message, sender_lookup) for message in messages]
    first_seq = messages[0].seq if messages else None
    last_seq = messages[-1].seq if messages else None
    return payload, first_seq, last_seq


def run_search(
    *,
    db: AgentBusDB,
    query: str,
    mode: str,
    limit: int,
    topic_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    from agent_bus.search import DEFAULT_EMBEDDING_MODEL, search_messages

    query = query.strip()
    if not query:
        return [], []

    mode_value = cast(SearchMode, mode.lower())
    results, warnings = search_messages(
        db,
        query=query,
        mode=mode_value,
        topic_id=topic_id,
        limit=max(1, min(limit, 50)),
        model=DEFAULT_EMBEDDING_MODEL,
    )
    return list(results), list(warnings)


def encode_sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def topics_version(db: AgentBusDB) -> int:
    return db.topic_list_version()


def format_export_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def topic_stream_state(db: AgentBusDB, *, topic_id: str) -> dict[str, Any]:
    summary = get_topic_summary(db, topic_id=topic_id)
    presence = db.get_presence(topic_id=topic_id, window_seconds=PRESENCE_WINDOW_SECONDS)
    return {
        "topic_id": topic_id,
        "last_seq": summary["last_seq"],
        "message_count": summary["message_count"],
        "presence": [serialize_cursor(item) for item in presence],
    }


@app.get("/api/topics")
async def api_topics(
    request: Request,
    status: TopicStatusFilter = "all",
    sort: TopicSort = "last_updated_desc",
    q: str = "",
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    db = get_db()
    auth = request_auth(request)
    fetch_limit = limit
    if auth is not None and not auth.admin:
        # ponytail: over-fetch + filter (core LIMIT is global); core WHERE owner=? if scale demands
        fetch_limit = limit * 4
    rows = db.topic_list_with_counts(status=status, sort=sort, query=q, limit=fetch_limit)
    if auth is not None and not auth.admin:
        rows = [row for row in rows if row_is_visible(auth, row)]
    topics = [normalize_topic_summary(row) for row in rows[:limit]]
    return {"topics": topics}


@app.get("/api/topics/{topic_id}")
async def api_topic_detail(
    request: Request, topic_id: str, focus: str | None = None
) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)
    summary = get_topic_summary(db, topic_id=topic_id)
    context_mode = False
    focus_message_id: str | None = None

    if focus:
        try:
            focused = db.get_message_by_id(message_id=focus)
        except ValueError:
            raise HTTPException(status_code=404, detail="Message not found") from None
        if focused.topic_id != topic_id:
            raise HTTPException(status_code=404, detail="Message not found") from None

        window = 25
        messages = db.get_messages(
            topic_id=topic_id,
            after_seq=max(0, focused.seq - window - 1),
            before_seq=focused.seq + window + 1,
            limit=(window * 2) + 1,
        )
        context_mode = True
        focus_message_id = focus
    else:
        messages = db.get_latest_messages(topic_id=topic_id, limit=DEFAULT_PAGE_SIZE)

    payload, first_seq, last_seq = serialize_topic_messages(db, messages)
    presence = db.get_presence(topic_id=topic_id, window_seconds=PRESENCE_WINDOW_SECONDS)

    return {
        "topic": summary,
        "messages": payload,
        "message_count": summary["message_count"],
        "first_seq": first_seq,
        "last_seq": last_seq,
        "has_earlier": bool(first_seq and first_seq > 1),
        "context_mode": context_mode,
        "focus_message_id": focus_message_id,
        "presence": [serialize_cursor(item) for item in presence],
    }


@app.get("/api/topics/{topic_id}/messages")
async def api_topic_messages(
    request: Request,
    topic_id: str,
    after_seq: int = Query(0, ge=0),
    before_seq: int | None = Query(None, ge=0),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=200),
) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)

    messages = db.get_messages(
        topic_id=topic_id,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
    )
    payload, first_seq, last_seq = serialize_topic_messages(db, messages)
    return {
        "messages": payload,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "has_earlier": bool(first_seq and first_seq > 1),
    }


@app.get("/api/search")
async def api_global_search(
    request: Request,
    q: str = "",
    mode: SearchMode = "hybrid",
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    db = get_db()
    auth = request_auth(request)
    fetch_limit = limit * 4 if (auth is not None and not auth.admin) else limit
    results, warnings = run_search(db=db, query=q, mode=mode, limit=fetch_limit)
    if auth is not None and not auth.admin:
        # ponytail: over-fetch + filter; core WHERE owner=? if scale demands
        owned_ids = {t.topic_id for t in db.topic_list(status="all") if is_visible(auth, t)}
        results = [r for r in results if r.get("topic_id") in owned_ids][:limit]
    return {"query": q.strip(), "mode": mode, "warnings": warnings, "results": results}


@app.get("/api/topics/{topic_id}/search")
async def api_topic_search(
    request: Request,
    topic_id: str,
    q: str = "",
    mode: SearchMode = "hybrid",
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)

    results, warnings = run_search(db=db, query=q, mode=mode, limit=limit, topic_id=topic_id)
    return {
        "topic_id": topic_id,
        "query": q.strip(),
        "mode": mode,
        "warnings": warnings,
        "results": results,
    }


@app.get("/api/topics/{topic_id}/export", response_class=PlainTextResponse)
async def api_topic_export(request: Request, topic_id: str) -> PlainTextResponse:
    db = get_db()
    guard_topic(request, topic_id)
    try:
        summary = get_topic_summary(db, topic_id=topic_id)
    except TopicNotFoundError:
        raise HTTPException(status_code=404, detail="Topic not found") from None

    messages = db.get_messages(topic_id=topic_id, after_seq=0, limit=10_000)
    lines = [
        f"# {summary['name']}",
        "",
        f"**Topic ID:** {summary['topic_id']}",
        f"**Status:** {summary['status']}",
        f"**Messages:** {summary['message_count']}",
        "",
        "---",
        "",
    ]
    for message in messages:
        lines.append(f"### [{message.seq}] {message.sender}")
        lines.append(f"*{format_export_timestamp(message.created_at)}*")
        if message.reply_to:
            lines.append(f"*Reply to: {message.reply_to}*")
        lines.append("")
        lines.append(message.content_markdown)
        lines.append("")
        lines.append("---")
        lines.append("")

    safe_name = "".join(
        char if char.isalnum() or char in ("-", "_") else "-" for char in summary["name"]
    )
    return PlainTextResponse(
        content="\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe_name or "topic"}.md"'},
    )


@app.post("/api/topics/{topic_id}/messages")
async def api_post_message(
    request: Request,
    topic_id: str,
    payload: PostMessageRequest,
) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)
    try:
        topic = db.get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        raise HTTPException(status_code=404, detail="Topic not found") from None

    if topic.status != "open":
        raise HTTPException(status_code=400, detail="Topic is closed")

    outbox_item = {
        "content_markdown": payload.content_markdown,
        "message_type": payload.message_type,
        "reply_to": payload.reply_to,
        "metadata": None,
        "client_message_id": None,
    }

    try:
        sent, _, _, _ = await asyncio.to_thread(
            db.sync_once,
            topic_id=topic_id,
            agent_name=payload.sender,
            outbox=[outbox_item],
            max_items=0,
            include_self=False,
            auto_advance=True,
            ack_through=None,
        )
    except DBBusyError:
        raise HTTPException(status_code=503, detail="Database is busy") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from None

    if not sent:
        raise HTTPException(status_code=500, detail="Failed to insert message")

    msg, dup = sent[0]
    sender_by_msg_id = db.get_senders_by_message_ids([msg.reply_to]) if msg.reply_to else {}
    return {
        "status": "ok",
        "message": serialize_message(msg, sender_by_msg_id),
        "duplicate": dup,
    }


@app.post("/api/topics/{topic_id}/close")
async def api_close_topic(
    request: Request,
    topic_id: str,
    payload: CloseTopicRequest | None = None,
) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)
    reason = payload.reason if payload and payload.reason is not None else "closed via web UI"
    try:
        _topic, closed_now = await asyncio.to_thread(
            db.topic_close,
            topic_id=topic_id,
            reason=reason,
        )
    except TopicNotFoundError:
        raise HTTPException(status_code=404, detail="Topic not found") from None
    except DBBusyError:
        raise HTTPException(status_code=503, detail="Database is busy") from None

    summary = get_topic_summary(db, topic_id=topic_id)
    return {
        "status": "ok",
        "topic": summary,
        "closed_now": closed_now,
    }


@app.delete("/api/topics/{topic_id}")
async def api_delete_topic(request: Request, topic_id: str) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)
    deleted = db.delete_topic(topic_id=topic_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Topic not found") from None
    return {"status": "ok", "topic_id": topic_id, "deleted": True}


@app.delete("/api/topics/{topic_id}/messages")
async def api_delete_messages(
    request: Request,
    topic_id: str,
    message_ids: Annotated[list[str], Body(embed=True)],
) -> dict[str, Any]:
    db = get_db()
    guard_topic(request, topic_id)

    deleted_message_ids = db.delete_messages_batch(topic_id=topic_id, message_ids=message_ids)
    return {
        "status": "ok",
        "deleted_count": len(deleted_message_ids),
        "deleted_message_ids": deleted_message_ids,
    }


@app.get("/api/stream/topics")
async def api_topics_stream(request: Request) -> StreamingResponse:
    db = get_db()

    async def event_stream():
        previous_version: int | None = None
        last_heartbeat = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    return
                if not token_still_valid(request):
                    return

                try:
                    version = topics_version(db)
                except DBBusyError:
                    if now() - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                        last_heartbeat = now()
                        yield encode_sse("heartbeat", {"timestamp": last_heartbeat})
                else:
                    if version != previous_version:
                        previous_version = version
                        last_heartbeat = now()
                        yield encode_sse("topics.invalidate", {"timestamp": last_heartbeat})
                    elif now() - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                        last_heartbeat = now()
                        yield encode_sse("heartbeat", {"timestamp": last_heartbeat})

                await asyncio.sleep(TOPICS_STREAM_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    return SSEStreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/stream/topics/{topic_id}")
async def api_topic_stream(topic_id: str, request: Request) -> StreamingResponse:
    db = get_db()
    guard_topic(request, topic_id)

    async def event_stream():
        previous_state: dict[str, Any] | None = None
        last_heartbeat = 0.0
        try:
            while True:
                if await request.is_disconnected():
                    return
                if not token_still_valid(request):
                    return

                try:
                    state = topic_stream_state(db, topic_id=topic_id)
                except TopicNotFoundError:
                    yield encode_sse("topic.deleted", {"topic_id": topic_id})
                    return
                except DBBusyError:
                    if now() - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                        last_heartbeat = now()
                        yield encode_sse("heartbeat", {"timestamp": last_heartbeat})
                else:
                    if state != previous_state:
                        previous_state = state
                        last_heartbeat = now()
                        yield encode_sse("topic.update", state)
                    elif now() - last_heartbeat >= STREAM_HEARTBEAT_SECONDS:
                        last_heartbeat = now()
                        yield encode_sse("heartbeat", {"timestamp": last_heartbeat})

                await asyncio.sleep(TOPIC_STREAM_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    return SSEStreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
async def spa_root() -> Response:
    return spa_index_response()


@app.get("/topics/{topic_id}", response_class=HTMLResponse)
async def spa_topic_page(topic_id: str) -> Response:
    _ = topic_id
    return spa_index_response()


@app.get("/{path:path}")
async def spa_assets(path: str) -> Response:
    if not path:
        return spa_index_response()

    candidate = (STATIC_DIR / path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None

    if candidate.is_file():
        return FileResponse(candidate)

    if "." in Path(path).name:
        raise HTTPException(status_code=404, detail="Not found") from None

    return spa_index_response()


def run_server(host: str = "127.0.0.1", port: int = 8080, db_path: str | None = None) -> None:
    import os

    import uvicorn

    if db_path:
        os.environ["AGENT_BUS_DB"] = db_path  # shared with agent_bus.peer_server.db
    init_db(db_path)
    setup_mcp_routes()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=SERVER_SHUTDOWN_GRACE_SECONDS,
    )
    with suppress(KeyboardInterrupt):
        ImmediateSigintServer(config).run()
