from __future__ import annotations

import asyncio
import contextlib
import time
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult
from pydantic import Field

from agent_bus.auth import AuthIdentity, identity_from_scope
from agent_bus.common import (
    ErrorCode,
    ToolWarning,
    WarningCode,
    env_int,
    env_str,
    json_loads,
    tool_error,
    tool_ok,
)
from agent_bus.db import (
    AgentBusDB,
    AgentNameInUseError,
    AgentNotJoinedError,
    DBBusyError,
    MutedError,
    PollClosedError,
    PollNotFoundError,
    SchemaMismatchError,
    TopicClosedError,
    TopicNotFoundError,
)
from agent_bus.ownership import (
    OWNER_KEY,
    is_visible,
    is_visible_topic_id,
    owner_key,
    resolve_owned_topic,
    sanitize_topic_metadata,
    visible_topics,
)
from agent_bus.tool_schemas import (
    ChairMuteOutput,
    ChairUnmuteOutput,
    CursorResetOutput,
    MessagesSearchOutput,
    PingOutput,
    PollCloseOutput,
    PollOpenOutput,
    PollStatusOutput,
    PollVoteOutput,
    SyncOutput,
    TopicCloseOutput,
    TopicCreateOutput,
    TopicJoinOutput,
    TopicListOutput,
    TopicPresenceOutput,
    TopicResolveOutput,
    TopicUpdateOutput,
)
from agent_bus.version import __version__

SPEC_VERSION = "v6.3"


def _schema_env_int(name: str, *, default: int, min_value: int) -> int:
    try:
        return env_int(name, default=default, min_value=min_value)
    except ValueError:
        return default


MAX_SYNC_ITEMS_LIMIT = _schema_env_int("AGENT_BUS_MAX_SYNC_ITEMS", default=20, min_value=1)
DEFAULT_SYNC_ITEMS = min(20, MAX_SYNC_ITEMS_LIMIT)
MAX_SYNC_ITEMS_DESCRIPTION = (
    "Maximum number of items to return. "
    f"Keep this small and no greater than {MAX_SYNC_ITEMS_LIMIT}; "
    "loop until has_more=false."
)

db = AgentBusDB()
mcp = FastMCP(
    name="agent-bus",
    instructions=(
        "Join a topic with topic_join(agent_name=..., topic_id=...|name=...), then use sync() to "
        f"read/write messages. Use small max_items (<= {MAX_SYNC_ITEMS_LIMIT}) and call sync "
        "repeatedly until has_more is false. If you need to replay history, call "
        "cursor_reset(topic_id=..., last_seq=0). "
        "Read messages from structuredContent.received[*].content_markdown. Some MCP clients do "
        "not expose structuredContent. In that case, use the sync() text output (it includes "
        "message bodies and may be truncated). Outbox items require "
        "content_markdown. Use reply_to to respond to a specific message. "
        "Convention: message_type='question' for questions and message_type='answer' for replies. "
        "Tip: use client_message_id to make retries idempotent."
    ),
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    json_response=True,
)


@dataclass(frozen=True, slots=True)
class JoinedIdentity:
    agent_name: str
    reclaim_token: str


# In-memory (per server process) mapping of joined topic_id -> agent identity.
# This is intentionally ephemeral: clients must call topic_join() again after a server restart.
_joined_identities: dict[str, JoinedIdentity] = {}


def _session_key(topic_id: str) -> str:
    try:
        ctx = mcp.get_context()
        session_id = getattr(ctx, "client_id", None) or (
            str(id(ctx.session)) if getattr(ctx, "session", None) is not None else None
        )
        if session_id:
            return f"{session_id}:{topic_id}"
    except (LookupError, ValueError, AttributeError):
        pass
    return topic_id


def _normalize_agent_name(agent_name: str) -> str:
    return agent_name.strip()


def _validate_agent_name(agent_name: object) -> str | None:
    if not isinstance(agent_name, str) or not agent_name.strip():
        return "agent_name must be a non-empty string"
    normalized = agent_name.strip()
    if len(normalized) > 64:
        return "agent_name must be <= 64 characters"
    if any(unicodedata.category(c) == "Cc" for c in normalized):
        return "agent_name must not contain control characters"
    return None


def _agent_name_for_topic(topic_id: str) -> str | None:
    identity = _joined_identities.get(_session_key(topic_id)) or _joined_identities.get(topic_id)
    return identity.agent_name if identity is not None else None


def _current_auth() -> AuthIdentity | None:
    """Authenticated identity of the calling HTTP request, or None.

    None on stdio (local-file trust: no HTTP request, no enforcement) and for
    any context-less invocation. A genuine HTTP request carries identity via
    TokenAuthMiddleware; absence there fails at the middleware, not here.
    """
    try:
        request = mcp.get_context().request_context.request
    except (LookupError, ValueError):
        return None
    if request is None:
        return None
    return identity_from_scope(request.scope)


def _suggest_agent_names(agent_name: str) -> list[str]:
    roles = ["reviewer", "frontend", "architect", "implementer", "researcher"]
    fallbacks = ["blue", "curious", "steady", "swift"]
    suggestions: list[str] = []
    seen = {agent_name}
    for role in roles:
        candidate = f"{agent_name} {role}"
        if candidate not in seen:
            seen.add(candidate)
            suggestions.append(candidate)
    for prefix in fallbacks:
        candidate = f"{prefix} {agent_name}"
        if candidate not in seen:
            seen.add(candidate)
            suggestions.append(candidate)
    return suggestions


def _topic_access_error(topic_id: str) -> Any | None:
    """Error result when the caller cannot see this topic; None when access is fine.

    Foreign topics are indistinguishable from missing ones (TOPIC_NOT_FOUND).
    stdio callers (no HTTP identity) and admins skip the check.
    """
    auth = _current_auth()
    if auth is None or auth.admin:
        return None
    try:
        if not is_visible_topic_id(db, auth, topic_id):
            return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    return None


def _schema_mismatch_result(e: SchemaMismatchError) -> Any:
    return tool_error(code=ErrorCode.DB_SCHEMA_MISMATCH, message=str(e))


def _truncate_for_tool_text(body: str, *, max_chars: int) -> tuple[str, bool]:
    if len(body) <= max_chars:
        return body, False
    return body[:max_chars] + "\n… (truncated)", True


@mcp.tool(description="Health check for the Agent Bus dialog MCP server.")
def ping() -> Annotated[CallToolResult, PingOutput]:
    """Health check for the Agent Bus dialog MCP server."""
    return tool_ok(
        text=f"pong ({__version__})",
        structured={"ok": True, "spec_version": SPEC_VERSION, "package_version": __version__},
    )


@mcp.tool(
    description=(
        "Create a topic. Use mode='new' for a fresh discussion thread, or mode='reuse' to "
        "return the newest open topic with the same name. After creating a fresh topic, call "
        "topic_join with the returned topic_id."
    )
)
def topic_create(
    name: Annotated[
        str | None,
        Field(
            description=(
                "Optional topic label. When mode='new', provide the name for the new topic. "
                "When mode='reuse', this is used to find the newest open topic with the same name."
            )
        ),
    ] = None,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description="Optional JSON object stored on the topic."),
    ] = None,
    mode: Annotated[
        Literal["reuse", "new"],
        Field(
            description=(
                "Topic creation mode. Use 'new' for a fresh topic. Use 'reuse' to return the "
                "newest open topic with the same name."
            )
        ),
    ] = "reuse",
) -> Annotated[CallToolResult, TopicCreateOutput]:
    """Create a topic (or reuse an existing open topic).

    mode:
    - reuse: return newest open topic with the same name
    - new: always create a new open topic
    """
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="metadata must be an object")

    auth = _current_auth()
    metadata = sanitize_topic_metadata(metadata, auth)

    try:
        # Non-admin reuse resolves within the caller's own topics only; a
        # foreign same-named topic is invisible and never handed over.
        if auth is not None and not auth.admin and mode == "reuse" and name:
            try:
                topic = resolve_owned_topic(db, auth, name=name, allow_closed=False)
                text = (
                    f'Topic: name="{topic.name}", topic_id="{topic.topic_id}", '
                    f'status="{topic.status}"'
                )
                return tool_ok(
                    text=text,
                    structured={
                        "topic_id": topic.topic_id,
                        "name": topic.name,
                        "status": topic.status,
                    },
                )
            except TopicNotFoundError:
                pass  # no owned open match: fall through and create a new topic

        topic = db.topic_create(
            name=name,
            metadata=metadata,
            mode=("new" if auth is not None and not auth.admin else mode),
        )
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    text = f'Topic: name="{topic.name}", topic_id="{topic.topic_id}", status="{topic.status}"'
    return tool_ok(
        text=text,
        structured={"topic_id": topic.topic_id, "name": topic.name, "status": topic.status},
    )


@mcp.tool(description="List topics in the shared Agent Bus DB.")
def topic_list(
    status: Literal["open", "closed", "all"] = "open",
) -> Annotated[CallToolResult, TopicListOutput]:
    """List topics in the shared Agent Bus DB (scoped to the caller's topics)."""
    try:
        topics = db.topic_list(status=status)
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    auth = _current_auth()
    topics = visible_topics(auth, topics)

    structured_topics = [
        {
            "topic_id": t.topic_id,
            "name": t.name,
            "status": t.status,
            "created_at": t.created_at,
            "closed_at": t.closed_at,
            "close_reason": t.close_reason,
            "metadata": t.metadata,
        }
        for t in topics
    ]
    lines = [f"Topics ({status}): {len(structured_topics)}"]
    for t in structured_topics[:20]:
        lines.append(f"- {t['name']} ({t['topic_id']}) status={t['status']}")
    if len(structured_topics) > 20:
        lines.append(f"... ({len(structured_topics) - 20} more)")
    return tool_ok(text="\n".join(lines), structured={"topics": structured_topics})


@mcp.tool(description="Search messages (FTS / semantic / hybrid).")
def messages_search(
    query: str,
    *,
    topic_id: str | None = None,
    mode: Literal["hybrid", "fts", "semantic"] = "hybrid",
    limit: int = 20,
    model: str | None = None,
    include_content: bool = False,
) -> Annotated[CallToolResult, MessagesSearchOutput]:
    """Search messages across topics or within a topic.

    This tool is read-only and does not require calling topic_join().
    """
    if not isinstance(query, str) or not query.strip():
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="query must be a non-empty string"
        )
    if topic_id is not None and (not isinstance(topic_id, str) or not topic_id):
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="topic_id must be a non-empty string"
        )
    if not isinstance(limit, int) or limit <= 0:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="limit must be > 0")
    if not isinstance(include_content, bool):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="include_content must be a bool")

    try:
        tool_text_include_bodies = (
            env_int("AGENT_BUS_TOOL_TEXT_INCLUDE_BODIES", default=1, min_value=0) != 0
        )
        tool_text_max_chars = env_int("AGENT_BUS_TOOL_TEXT_MAX_CHARS", default=64000, min_value=80)
    except ValueError as e:  # pragma: no cover
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    try:
        from agent_bus.search import DEFAULT_EMBEDDING_MODEL, search_messages

        default_model = env_str("AGENT_BUS_EMBEDDING_MODEL", default=DEFAULT_EMBEDDING_MODEL)

        auth = _current_auth()
        fetch_limit = limit
        if auth is not None and not auth.admin:
            if topic_id is not None:
                if not is_visible_topic_id(db, auth, topic_id):
                    return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
            else:
                # ponytail: over-fetch + post-filter for scoped global search;
                # core WHERE owner=? if scale demands.
                fetch_limit = limit * 4

        results, warnings_list = search_messages(
            db,
            query=query,
            mode=mode,
            topic_id=topic_id,
            limit=fetch_limit,
            model=model or default_model,
            include_content=include_content,
        )

        if auth is not None and not auth.admin and topic_id is None:
            owned_ids = {t.topic_id for t in visible_topics(auth, db.topic_list(status="all"))}
            results = [r for r in results if r.get("topic_id") in owned_ids][:limit]
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    except (ValueError, RuntimeError) as e:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    warnings: list[ToolWarning] = [ToolWarning(code=w) for w in warnings_list]
    lines = [f"Search: mode={mode} results={len(results)}"]
    if include_content and not tool_text_include_bodies:
        lines.append(
            "Note: include_content=true adds content_markdown to structuredContent. "
            "Tool text output may omit bodies depending on server configuration."
        )
        lines.append("")
    for r in results[:20]:
        meta: list[str] = []
        fts_rank = r.get("fts_rank", r.get("rank"))
        if fts_rank is not None:
            meta.append(f"fts_rank={fts_rank}")
        semantic_score = r.get("semantic_score")
        if semantic_score is not None:
            meta.append(f"semantic_score={semantic_score}")
        meta_str = f" ({', '.join(meta)})" if meta else ""

        lines.append(
            f"- {r['topic_name']} [{r['seq']}] {r['sender']} ({r['message_type']}) "
            f"id={r['message_id']}{meta_str}"
        )

        if (
            include_content
            and tool_text_include_bodies
            and isinstance(r.get("content_markdown"), str)
        ):
            body, truncated = _truncate_for_tool_text(
                r["content_markdown"], max_chars=tool_text_max_chars
            )
            lines.append(body)
            if truncated:
                lines.append(f"(truncated; {len(r['content_markdown'])} chars total)")
        else:
            snippet = str(r.get("snippet") or "").splitlines()[0][:80]
            lines.append(snippet)
        lines.append("")
    if len(results) > 20:
        lines.append(f"... ({len(results) - 20} more)")

    return tool_ok(
        text="\n".join(lines),
        structured={
            "query": query,
            "mode": mode,
            "topic_id": topic_id,
            "include_content": include_content,
            "results": results,
            "count": len(results),
        },
        warnings=warnings or None,
    )


@mcp.tool(description="Close a topic (idempotent).")
def topic_close(
    topic_id: str, reason: str | None = None
) -> Annotated[CallToolResult, TopicCloseOutput]:
    """Close a topic (idempotent)."""
    err = _topic_access_error(topic_id)
    if err is not None:
        return err
    try:
        topic, already_closed = db.topic_close(topic_id=topic_id, reason=reason)
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    warnings: list[ToolWarning] = []
    if already_closed:
        warnings.append(
            ToolWarning(code=str(WarningCode.ALREADY_CLOSED), context={"topic_id": topic_id})
        )

    text = f'Topic closed: topic_id="{topic.topic_id}" closed_at={topic.closed_at}'
    if topic.close_reason:
        text += f' close_reason="{topic.close_reason}"'
    return tool_ok(
        text=text,
        structured={
            "topic_id": topic.topic_id,
            "status": topic.status,
            "closed_at": topic.closed_at,
            "close_reason": topic.close_reason,
        },
        warnings=warnings or None,
    )


@mcp.tool(description="Resolve a topic by name.")
def topic_resolve(
    name: str, allow_closed: bool = False
) -> Annotated[CallToolResult, TopicResolveOutput]:
    """Resolve a topic by name."""
    if not isinstance(name, str) or not name:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="name must be a non-empty string"
        )
    auth = _current_auth()
    try:
        if auth is not None and not auth.admin:
            topic = resolve_owned_topic(db, auth, name=name, allow_closed=allow_closed)
        else:
            topic = db.topic_resolve(name=name, allow_closed=allow_closed)
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    return tool_ok(
        text=f'Topic resolved: name="{topic.name}" topic_id="{topic.topic_id}" status="{topic.status}"',
        structured={"topic_id": topic.topic_id, "name": topic.name, "status": topic.status},
    )


@mcp.tool(
    description=(
        "Join a topic as a named peer for this MCP session. Provide exactly one of topic_id or "
        "name. Duplicate agent names are rejected for the life of the topic. Persist the returned "
        "reclaim_token if you need to reuse the same agent_name after a restart. Typical flow "
        "after topic_create(mode='new'): join with the returned topic_id before calling sync."
    )
)
def topic_join(
    agent_name: Annotated[
        str,
        Field(
            description=(
                "Your peer name for this topic, for example 'reviewer'. The name stays reserved "
                "for the life of the topic once claimed."
            )
        ),
    ],
    *,
    topic_id: Annotated[
        str | None,
        Field(description="Topic id to join. Prefer this after topic_create(mode='new')."),
    ] = None,
    name: Annotated[
        str | None,
        Field(description="Topic name to resolve and join when topic_id is not provided."),
    ] = None,
    allow_closed: Annotated[
        bool,
        Field(description="Allow joining a closed topic when resolving by name."),
    ] = False,
    reclaim_token: Annotated[
        str | None,
        Field(
            description=(
                "Opaque reclaim_token returned by a prior successful topic_join(). Provide it to "
                "reclaim the same agent_name after a restart or reconnect."
            )
        ),
    ] = None,
) -> Annotated[CallToolResult, TopicJoinOutput]:
    """Join a topic as a named peer (in-memory per server process).

    Requires joining before calling `sync()`.
    Exactly one of `topic_id` or `name` must be provided.
    """
    err = _validate_agent_name(agent_name)
    if err:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=err)
    if reclaim_token is not None:
        if not isinstance(reclaim_token, str) or reclaim_token.strip() == "":
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT, message="reclaim_token must be a non-empty string"
            )
        reclaim_token = reclaim_token.strip()

    if topic_id and name:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="Provide exactly one of topic_id or name"
        )
    if not topic_id and not name:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="Provide topic_id or name")

    auth = _current_auth()
    try:
        if topic_id:
            if not isinstance(topic_id, str) or not topic_id:
                return tool_error(
                    code=ErrorCode.INVALID_ARGUMENT, message="topic_id must be a non-empty string"
                )
            topic = db.get_topic(topic_id=topic_id)
            if auth is not None and not auth.admin and not is_visible(auth, topic):
                return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
        else:
            if not isinstance(name, str) or not name:
                return tool_error(
                    code=ErrorCode.INVALID_ARGUMENT, message="name must be a non-empty string"
                )
            if auth is not None and not auth.admin:
                # Foreign same-named topics are invisible: resolve within owned only.
                topic = resolve_owned_topic(
                    db, auth, name=cast(str, name), allow_closed=allow_closed
                )
            else:
                topic = db.topic_resolve(name=cast(str, name), allow_closed=allow_closed)
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    normalized = _normalize_agent_name(agent_name)
    s_key = _session_key(topic.topic_id)
    existing = _joined_identities.get(s_key) or _joined_identities.get(topic.topic_id)
    if existing is not None and existing.agent_name == normalized:
        reclaim = existing.reclaim_token
    else:
        try:
            normalized, reclaim = db.reserve_agent_name(
                topic_id=topic.topic_id,
                agent_name=normalized,
                reclaim_token=reclaim_token,
            )
        except SchemaMismatchError as e:
            return _schema_mismatch_result(e)
        except TopicNotFoundError:
            return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
        except DBBusyError:
            return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
        except AgentNameInUseError:
            return tool_error(
                code=ErrorCode.AGENT_NAME_IN_USE,
                message=(
                    f'agent_name "{normalized}" is already reserved for this topic. '
                    "Provide the original reclaim_token to reuse it, or choose a different "
                    "agent_name."
                ),
                structured={
                    "requested_agent_name": normalized,
                    "suggested_agent_names": _suggest_agent_names(normalized),
                },
            )
        identity = JoinedIdentity(
            agent_name=normalized,
            reclaim_token=reclaim,
        )
        _joined_identities[s_key] = identity
        _joined_identities[topic.topic_id] = identity

    text = (
        f'Joined topic "{topic.name}" ({topic.topic_id}) as "{normalized}".\n'
        f"reclaim_token={reclaim}"
    )
    return tool_ok(
        text=text,
        structured={
            "topic_id": topic.topic_id,
            "name": topic.name,
            "status": topic.status,
            "agent_name": normalized,
            "reclaim_token": reclaim,
        },
    )


@mcp.tool(description="List peers recently active on a topic (based on sync cursor activity).")
def topic_presence(
    topic_id: str, window_seconds: int = 300, limit: int = 200
) -> Annotated[CallToolResult, TopicPresenceOutput]:
    """List peers recently active on a topic.

    Presence is derived from the `cursors` table. A peer becomes "active" when it calls `sync()`,
    because `sync_once()` always touches `cursors.updated_at` for that `(topic_id, agent_name)`.
    """
    if not isinstance(topic_id, str) or not topic_id:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="topic_id must be a non-empty string"
        )
    if not isinstance(window_seconds, int) or window_seconds <= 0:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="window_seconds must be > 0")
    if not isinstance(limit, int) or limit <= 0:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="limit must be > 0")

    err = _topic_access_error(topic_id)
    if err is not None:
        return err

    try:
        cursors = db.get_presence(topic_id=topic_id, window_seconds=window_seconds, limit=limit)
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    except ValueError as e:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    now_ts = time.time()
    peers = [
        {
            "agent_name": c.agent_name,
            "last_seq": c.last_seq,
            "updated_at": c.updated_at,
            "age_seconds": max(0.0, now_ts - c.updated_at),
        }
        for c in cursors
    ]

    lines = [f"Presence: {len(peers)} active peer(s) in last {window_seconds}s"]
    for p in peers[:20]:
        age = p["age_seconds"]
        lines.append(f"- {p['agent_name']} last_seq={p['last_seq']} age={age:.1f}s")
    if len(peers) > 20:
        lines.append(f"... ({len(peers) - 20} more)")

    return tool_ok(
        text="\n".join(lines),
        structured={
            "topic_id": topic_id,
            "window_seconds": window_seconds,
            "limit": limit,
            "now": now_ts,
            "peers": peers,
            "count": len(peers),
        },
    )


@mcp.tool(description="Reset/set the server-side cursor for the joined peer on a topic.")
def cursor_reset(
    topic_id: str, *, agent_name: str | None = None, last_seq: int = 0
) -> Annotated[CallToolResult, CursorResetOutput]:
    """Reset/set the server-side cursor for this peer on a topic.

    Set last_seq=0 to replay the full history.
    """
    if not isinstance(topic_id, str) or not topic_id:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="topic_id must be a non-empty string"
        )

    if agent_name is None:
        agent_name = _agent_name_for_topic(topic_id)
    else:
        err = _validate_agent_name(agent_name)
        if err:
            return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=err)
        agent_name = agent_name.strip()

    if agent_name is None:
        return tool_error(
            code=ErrorCode.AGENT_NOT_JOINED,
            message="Not joined to topic. Call topic_join() first or pass agent_name.",
        )

    if not isinstance(last_seq, int) or last_seq < 0:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="last_seq must be an int >= 0")

    err = _topic_access_error(topic_id)
    if err is not None:
        return err

    try:
        cursor = db.cursor_set(topic_id=topic_id, agent_name=agent_name, last_seq=last_seq)
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    except ValueError as e:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    return tool_ok(
        text=f'Cursor set: topic_id="{topic_id}" agent_name="{agent_name}" last_seq={cursor.last_seq}',
        structured={
            "topic_id": topic_id,
            "agent_name": agent_name,
            "cursor": {"last_seq": cursor.last_seq, "updated_at": cursor.updated_at},
        },
    )


@mcp.tool(
    description=(
        "Sync messages on a topic (delta-based, read/write, server-side cursor). "
        "Use structuredContent.received[*].content_markdown for full message bodies. Some clients "
        "only show text output; the text output includes message bodies (may be truncated)."
    )
)
async def sync(
    topic_id: str,
    *,
    agent_name: Annotated[
        str | None,
        Field(
            description="Your agent name. If omitted, uses the identity registered via topic_join().",
        ),
    ] = None,
    outbox: Annotated[
        list[dict[str, Any]] | dict[str, Any] | str | None,
        Field(
            description=(
                "Outgoing messages to send. Each item is an object with: "
                "content_markdown (string, required), message_type (string, optional, default "
                '"message"), reply_to (string|null), metadata (object|null), client_message_id '
                "(string|null, optional idempotency key)."
            ),
            json_schema_extra={
                "examples": [
                    [
                        {
                            "content_markdown": "Hello from red-squirrel",
                            "message_type": "message",
                            "reply_to": None,
                            "metadata": {"kind": "greeting"},
                            "client_message_id": "msg-001",
                        }
                    ]
                ]
            },
        ),
    ] = None,
    max_items: Annotated[
        int,
        Field(
            description=MAX_SYNC_ITEMS_DESCRIPTION,
            ge=1,
            le=MAX_SYNC_ITEMS_LIMIT,
        ),
    ] = DEFAULT_SYNC_ITEMS,
    include_self: bool = False,
    wait_seconds: int = 60,
    auto_advance: bool = True,
    ack_through: int | None = None,
) -> Annotated[CallToolResult, SyncOutput]:
    """Read/write sync against a topic message stream.

    Requires joining the topic first via `topic_join()`.
    """
    if not isinstance(topic_id, str) or not topic_id:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="topic_id must be a non-empty string"
        )

    err = _topic_access_error(topic_id)
    if err is not None:
        return err

    if agent_name is None:
        agent_name = _agent_name_for_topic(topic_id)
    else:
        err = _validate_agent_name(agent_name)
        if err:
            return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=err)
        agent_name = agent_name.strip()

    if agent_name is None:
        return tool_error(
            code=ErrorCode.AGENT_NOT_JOINED,
            message="Not joined to topic. Call topic_join() first or pass agent_name.",
        )

    if outbox is None:
        outbox = []
    elif isinstance(outbox, str):
        try:
            outbox = json_loads(outbox.strip())
        except Exception:
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=(
                    "outbox must be a list of objects. You passed a string; pass an array "
                    "directly (no quotes), or pass valid JSON."
                ),
            )
    elif isinstance(outbox, dict):
        outbox = [outbox]
    if not isinstance(outbox, list):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="outbox must be a list")

    # Fast-write default: if posting outbox messages and caller left wait_seconds at 60s, don't block
    if outbox and wait_seconds == 60:
        wait_seconds = 0

    if not isinstance(max_items, int) or max_items <= 0:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="max_items must be a positive int"
        )
    if not isinstance(wait_seconds, int) or wait_seconds < 0:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="wait_seconds must be an int >= 0"
        )
    if not isinstance(include_self, bool):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="include_self must be a bool")
    if not isinstance(auto_advance, bool):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="auto_advance must be a bool")
    if ack_through is not None and (not isinstance(ack_through, int) or ack_through < 0):
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT, message="ack_through must be an int >= 0"
        )
    if ack_through is not None and auto_advance:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT,
            message="ack_through requires auto_advance=false",
        )

    try:
        max_outbox = env_int("AGENT_BUS_MAX_OUTBOX", default=50, min_value=0)
        max_message_chars = env_int("AGENT_BUS_MAX_MESSAGE_CHARS", default=65536, min_value=1)
        max_sync_items = env_int("AGENT_BUS_MAX_SYNC_ITEMS", default=20, min_value=1)
        poll_initial_ms = env_int("AGENT_BUS_POLL_INITIAL_MS", default=250, min_value=1)
        poll_max_ms = env_int("AGENT_BUS_POLL_MAX_MS", default=1000, min_value=1)
        tool_text_include_bodies = (
            env_int("AGENT_BUS_TOOL_TEXT_INCLUDE_BODIES", default=1, min_value=0) != 0
        )
        tool_text_max_chars = env_int("AGENT_BUS_TOOL_TEXT_MAX_CHARS", default=64000, min_value=80)
    except ValueError as e:  # pragma: no cover
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    if max_items > max_sync_items:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT,
            message=(
                f"max_items must be <= {max_sync_items}. "
                "Tip: keep max_items small and call sync repeatedly until has_more=false."
            ),
        )
    if len(outbox) > max_outbox:
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT,
            message=f"outbox must have at most {max_outbox} items",
        )

    sanitized: list[dict[str, Any]] = []
    for idx, item in enumerate(outbox):
        if not isinstance(item, dict):
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT, message=f"outbox[{idx}] must be an object"
            )

        content = item.get("content_markdown")
        if not isinstance(content, str) or not content:
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].content_markdown must be a non-empty string",
            )
        if len(content) > max_message_chars:
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].content_markdown exceeds max length ({max_message_chars})",
            )

        message_type = item.get("message_type", "message")
        if not isinstance(message_type, str) or not message_type.strip():
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].message_type must be a non-empty string",
            )
        message_type = message_type.strip()
        if len(message_type) > 32:
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].message_type must be <= 32 characters",
            )

        reply_to = item.get("reply_to")
        if reply_to is not None and (not isinstance(reply_to, str) or not reply_to):
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].reply_to must be a non-empty string or null",
            )

        metadata = item.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].metadata must be an object",
            )

        client_message_id = item.get("client_message_id")
        if client_message_id is not None and (
            not isinstance(client_message_id, str) or not client_message_id.strip()
        ):
            return tool_error(
                code=ErrorCode.INVALID_ARGUMENT,
                message=f"outbox[{idx}].client_message_id must be a non-empty string or null",
            )
        if isinstance(client_message_id, str):
            client_message_id = client_message_id.strip()
            if len(client_message_id) > 128:
                return tool_error(
                    code=ErrorCode.INVALID_ARGUMENT,
                    message=f"outbox[{idx}].client_message_id must be <= 128 characters",
                )

        sanitized.append(
            {
                "content_markdown": content,
                "message_type": message_type,
                "reply_to": reply_to,
                "metadata": metadata,
                "client_message_id": client_message_id,
            }
        )

    sent: list[tuple[Any, bool]] = []
    received = []
    cursor = None
    has_more = False
    tool_warnings: list[ToolWarning] = []

    try:
        sent, received, cursor, has_more = await asyncio.to_thread(
            db.sync_once,
            topic_id=topic_id,
            agent_name=agent_name,
            outbox=sanitized,
            max_items=max_items,
            include_self=include_self,
            auto_advance=auto_advance,
            ack_through=ack_through,
        )
    except SchemaMismatchError as e:
        return _schema_mismatch_result(e)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
    except TopicClosedError:
        return tool_error(code=ErrorCode.TOPIC_CLOSED, message="Topic is closed.")
    except MutedError as e:
        return tool_error(code=ErrorCode.MUTED, message=str(e))
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")
    except ValueError as e:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    # Opportunistically enqueue embeddings indexing for newly-created messages.
    # This is best-effort: failures should not affect message delivery.
    try:
        from agent_bus.embedding_worker import (
            autoindex_enabled,
            embedding_model,
        )

        if autoindex_enabled():
            model = embedding_model()
            jobs = [(m.message_id, m.topic_id) for m, dup in sent if not dup]
            if jobs:
                db.enqueue_embedding_jobs(jobs=jobs, model=model)
    except SchemaMismatchError as e:
        tool_warnings.append(ToolWarning(code="embeddings_schema_mismatch", message=str(e)))
    except DBBusyError:
        tool_warnings.append(
            ToolWarning(code="embeddings_enqueue_db_busy", message="Database busy; skipped enqueue")
        )
    except Exception as e:  # pragma: no cover
        tool_warnings.append(ToolWarning(code="embeddings_enqueue_failed", message=str(e)))

    deadline = time.monotonic() + wait_seconds
    interval_s = poll_initial_ms / 1000.0
    max_interval_s = poll_max_ms / 1000.0

    while not received and wait_seconds > 0 and time.monotonic() < deadline:
        await asyncio.sleep(interval_s)
        interval_s = min(interval_s * 2, max_interval_s)
        try:
            _, received, cursor, has_more = await asyncio.to_thread(
                db.sync_once,
                topic_id=topic_id,
                agent_name=agent_name,
                outbox=[],
                max_items=max_items,
                include_self=include_self,
                auto_advance=auto_advance,
                ack_through=None,
            )
        except DBBusyError:
            continue
        except SchemaMismatchError as e:
            return _schema_mismatch_result(e)
        except TopicNotFoundError:
            return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")
        except ValueError as e:
            return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))

    status: Literal["ready", "timeout", "empty"]
    if received:
        status = "ready"
    elif wait_seconds > 0:
        status = "timeout"
    else:
        status = "empty"

    def _msg_struct(m: Any) -> dict[str, Any]:
        return {
            "message_id": m.message_id,
            "topic_id": m.topic_id,
            "seq": m.seq,
            "sender": m.sender,
            "message_type": m.message_type,
            "reply_to": m.reply_to,
            "content_markdown": m.content_markdown,
            "metadata": m.metadata,
            "client_message_id": m.client_message_id,
            "created_at": m.created_at,
        }

    structured_sent = [{"message": _msg_struct(m), "duplicate": dup} for m, dup in sent]
    structured_received = [_msg_struct(m) for m in received]

    topic_metadata: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        topic_metadata = db.get_topic(topic_id=topic_id).metadata

    assert cursor is not None
    structured = {
        "topic_id": topic_id,
        "agent_name": agent_name,
        "status": status,
        "cursor": {"last_seq": cursor.last_seq, "updated_at": cursor.updated_at},
        "sent": structured_sent,
        "received": structured_received,
        "received_count": len(structured_received),
        "has_more": has_more,
        "topic_metadata": topic_metadata,
    }

    lines = [
        f"Sync: status={status} received={len(structured_received)} sent={len(structured_sent)} "
        f"cursor={cursor.last_seq} has_more={has_more}"
    ]

    for m in structured_received[:20]:
        header = f"[{m['seq']}] {m['sender']} ({m['message_type']}) id={m['message_id']}" + (
            f" reply_to={m['reply_to']}" if m.get("reply_to") else ""
        )
        if tool_text_include_bodies:
            lines.append(header)
            body, truncated = _truncate_for_tool_text(
                m["content_markdown"], max_chars=tool_text_max_chars
            )
            lines.append(body)
            if truncated:
                lines.append(f"(truncated; {len(m['content_markdown'])} chars total)")
            lines.append("")
        else:
            preview = m["content_markdown"].splitlines()[0][:80]
            lines.append(f"{header}: {preview}")
    if len(structured_received) > 20:
        lines.append(f"... ({len(structured_received) - 20} more)")

    return tool_ok(text="\n".join(lines), structured=structured, warnings=tool_warnings or None)


@mcp.tool(
    description=(
        "Update metadata on a topic. On a chaired topic, only the chair may update metadata "
        "(including chair handover or unchaired revert)."
    )
)
def topic_update(
    topic_id: str,
    caller: Annotated[str, Field(description="Your agent name.")],
    metadata: Annotated[dict[str, Any], Field(description="New topic metadata object.")],
) -> Annotated[CallToolResult, TopicUpdateOutput]:
    """Update topic metadata (chair only on chaired topics)."""
    err = _topic_access_error(topic_id)
    if err:
        return err

    try:
        topic = db.get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")

    if topic.status != "open":
        return tool_error(code=ErrorCode.TOPIC_CLOSED, message="Topic is closed.")

    if not isinstance(metadata, dict):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="metadata must be a dictionary.")

    existing_meta = topic.metadata or {}
    existing_chair = existing_meta.get("chair")
    if existing_chair and caller.strip() != existing_chair:
        return tool_error(
            code=ErrorCode.NOT_CHAIR,
            message=f"Only the chair ({existing_chair}) may update topic metadata.",
        )

    clean_meta = {k: v for k, v in metadata.items() if not k.startswith("_")}
    auth = _current_auth()
    if auth is not None:
        clean_meta[OWNER_KEY] = owner_key(auth)
    elif OWNER_KEY in existing_meta:
        clean_meta[OWNER_KEY] = existing_meta[OWNER_KEY]

    try:
        db.topic_update_metadata(topic_id=topic_id, metadata=clean_meta)
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    return tool_ok(
        text=f'Topic metadata updated: topic_id="{topic_id}"',
        structured={"topic_id": topic_id, "metadata": clean_meta},
    )


@mcp.tool(
    description=(
        "Mute a peer on a chaired topic (chair only). Muted peers cannot post messages to the topic."
    )
)
def chair_mute(
    topic_id: str,
    caller: Annotated[str, Field(description="Your agent name (must be the topic chair).")],
    target: Annotated[str, Field(description="Agent name to mute.")],
) -> Annotated[CallToolResult, ChairMuteOutput]:
    """Mute a peer on a chaired topic (chair only)."""
    err = _topic_access_error(topic_id)
    if err:
        return err

    try:
        topic = db.get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")

    if topic.status != "open":
        return tool_error(code=ErrorCode.TOPIC_CLOSED, message="Topic is closed.")

    meta = dict(topic.metadata or {})
    chair = meta.get("chair")
    if not chair:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="Topic is not chaired.")
    if caller.strip() != chair:
        return tool_error(code=ErrorCode.NOT_CHAIR, message=f"Only the chair ({chair}) may mute peers.")

    target_name = target.strip()
    if not target_name:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="target must be a non-empty string.")
    if target_name == chair:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="The chair cannot be muted.")

    muted = list(meta.get("muted") or [])
    if target_name not in muted:
        muted.append(target_name)
    meta["muted"] = muted

    try:
        db.topic_update_metadata(topic_id=topic_id, metadata=meta)
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    notice = f"SYSTEM: {target_name} has been muted by chair {caller.strip()}."
    with contextlib.suppress(Exception):
        db.sync_once(
            topic_id=topic_id,
            agent_name=caller.strip(),
            outbox=[{"content_markdown": notice, "message_type": "system"}],
            max_items=0,
            include_self=False,
            auto_advance=False,
            ack_through=None,
        )

    return tool_ok(
        text=notice,
        structured={"topic_id": topic_id, "target": target_name, "muted": True},
    )


@mcp.tool(
    description=(
        "Unmute a peer on a chaired topic (chair only). Restores posting ability for the peer."
    )
)
def chair_unmute(
    topic_id: str,
    caller: Annotated[str, Field(description="Your agent name (must be the topic chair).")],
    target: Annotated[str, Field(description="Agent name to unmute.")],
) -> Annotated[CallToolResult, ChairUnmuteOutput]:
    """Unmute a peer on a chaired topic (chair only)."""
    err = _topic_access_error(topic_id)
    if err:
        return err

    try:
        topic = db.get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")

    if topic.status != "open":
        return tool_error(code=ErrorCode.TOPIC_CLOSED, message="Topic is closed.")

    meta = dict(topic.metadata or {})
    chair = meta.get("chair")
    if not chair:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="Topic is not chaired.")
    if caller.strip() != chair:
        return tool_error(code=ErrorCode.NOT_CHAIR, message=f"Only the chair ({chair}) may unmute peers.")

    target_name = target.strip()
    if not target_name:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="target must be a non-empty string.")

    muted = [m for m in (meta.get("muted") or []) if m != target_name]
    meta["muted"] = muted

    try:
        db.topic_update_metadata(topic_id=topic_id, metadata=meta)
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    notice = f"SYSTEM: {target_name} has been unmuted by chair {caller.strip()}."
    with contextlib.suppress(Exception):
        db.sync_once(
            topic_id=topic_id,
            agent_name=caller.strip(),
            outbox=[{"content_markdown": notice, "message_type": "system"}],
            max_items=0,
            include_self=False,
            auto_advance=False,
            ack_through=None,
        )

    return tool_ok(
        text=notice,
        structured={"topic_id": topic_id, "target": target_name, "muted": False},
    )


@mcp.tool(
    description=(
        "Open a poll on a chaired topic (chair only). One open poll per topic; opening a second "
        "poll supersedes any earlier open poll. Posts an announcement message to the topic."
    )
)
def poll_open(
    topic_id: str,
    caller: Annotated[str, Field(description="Your agent name (must be the topic chair).")],
    question: Annotated[str, Field(description="The poll question to decide.")],
    options: Annotated[
        list[str] | None,
        Field(description="List of choices (default: ['yay', 'nay', 'abstain'])."),
    ] = None,
    threshold: Annotated[
        Literal["majority", "two-thirds", "plurality"],
        Field(description="Voting threshold required to adopt (default: 'majority')."),
    ] = "majority",
) -> Annotated[CallToolResult, PollOpenOutput]:
    """Open a poll on a chaired topic (chair only)."""
    err = _topic_access_error(topic_id)
    if err:
        return err

    try:
        topic = db.get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")

    if topic.status != "open":
        return tool_error(code=ErrorCode.TOPIC_CLOSED, message="Topic is closed.")

    chair = (topic.metadata or {}).get("chair")
    if not chair:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="Topic is not chaired.")
    if caller.strip() != chair:
        return tool_error(code=ErrorCode.NOT_CHAIR, message=f"Only the chair ({chair}) may open a poll.")

    q = question.strip()
    if not q:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="question must be a non-empty string.")

    opts = options if options is not None else ["yay", "nay", "abstain"]
    if not isinstance(opts, list) or len(opts) < 2:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="options must contain at least 2 choices.")
    if len(set(opts)) != len(opts):
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="options must be unique.")
    for opt in opts:
        if not isinstance(opt, str) or not opt.strip():
            return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="each option must be a non-empty string.")

    if threshold not in ("majority", "two-thirds", "plurality"):
        return tool_error(
            code=ErrorCode.INVALID_ARGUMENT,
            message="threshold must be 'majority', 'two-thirds', or 'plurality'.",
        )

    try:
        created = db.poll_create(
            topic_id=topic_id,
            question=q,
            options=opts,
            threshold=threshold,
            created_by=caller.strip(),
        )
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    poll_id = created["poll_id"]
    announcement = (
        f"POLL OPENED: {q}\n"
        f"Poll ID: {poll_id}\n"
        f"Options: {', '.join(opts)}\n"
        f"Threshold: {threshold}\n"
        f'Vote with poll_vote(poll_id="{poll_id}", caller="<your_name>", choice="<option>")'
    )
    with contextlib.suppress(Exception):
        db.sync_once(
            topic_id=topic_id,
            agent_name=caller.strip(),
            outbox=[{
                "content_markdown": announcement,
                "message_type": "system",
                "metadata": {"poll_id": poll_id, "options": opts, "threshold": threshold},
            }],
            max_items=0,
            include_self=False,
            auto_advance=False,
            ack_through=None,
        )

    return tool_ok(
        text=announcement,
        structured={
            "poll_id": poll_id,
            "topic_id": topic_id,
            "question": q,
            "options": opts,
            "threshold": threshold,
            "status": "open",
        },
    )


@mcp.tool(
    description=(
        "Cast a vote in an open poll. Each joined peer has exactly one changeable vote (last vote wins)."
    )
)
def poll_vote(
    poll_id: str,
    caller: Annotated[str, Field(description="Your agent name (must be joined to the topic).")],
    choice: Annotated[str, Field(description="Your chosen option.")],
) -> Annotated[CallToolResult, PollVoteOutput]:
    """Cast a vote in an open poll."""
    caller_name = caller.strip()
    if not caller_name:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="caller must be a non-empty string.")
    choice_val = choice.strip()
    if not choice_val:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="choice must be a non-empty string.")

    try:
        db.poll_vote(poll_id=poll_id, caller=caller_name, choice=choice_val)
    except PollNotFoundError:
        return tool_error(code=ErrorCode.POLL_NOT_FOUND, message=f"Poll '{poll_id}' not found.")
    except PollClosedError as e:
        return tool_error(code=ErrorCode.POLL_CLOSED, message=str(e))
    except AgentNotJoinedError as e:
        return tool_error(code=ErrorCode.AGENT_NOT_JOINED, message=str(e))
    except ValueError as e:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message=str(e))
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    return tool_ok(
        text=f"Vote recorded for {caller_name}: {choice_val} in poll {poll_id}",
        structured={"poll_id": poll_id, "caller": caller_name, "choice": choice_val},
    )


@mcp.tool(
    description=(
        "Close an open poll and record the final tally and verdict into the topic (chair only)."
    )
)
def poll_close(
    poll_id: str,
    caller: Annotated[str, Field(description="Your agent name (must be the topic chair).")],
) -> Annotated[CallToolResult, PollCloseOutput]:
    """Close an open poll (chair only)."""
    poll_data = db.poll_get(poll_id=poll_id)
    if poll_data is None:
        return tool_error(code=ErrorCode.POLL_NOT_FOUND, message=f"Poll '{poll_id}' not found.")

    if poll_data["status"] != "open":
        return tool_error(
            code=ErrorCode.POLL_CLOSED,
            message=f"Poll '{poll_id}' is already closed (status: {poll_data['status']}).",
        )

    err = _topic_access_error(poll_data["topic_id"])
    if err:
        return err

    try:
        topic = db.get_topic(topic_id=poll_data["topic_id"])
    except TopicNotFoundError:
        return tool_error(code=ErrorCode.TOPIC_NOT_FOUND, message="Topic not found.")

    chair = (topic.metadata or {}).get("chair")
    if not chair:
        return tool_error(code=ErrorCode.INVALID_ARGUMENT, message="Topic is not chaired.")
    if caller.strip() != chair:
        return tool_error(code=ErrorCode.NOT_CHAIR, message=f"Only the chair ({chair}) may close this poll.")

    try:
        result = db.poll_close(poll_id=poll_id)
    except PollNotFoundError:
        return tool_error(code=ErrorCode.POLL_NOT_FOUND, message=f"Poll '{poll_id}' not found.")
    except PollClosedError as e:
        return tool_error(code=ErrorCode.POLL_CLOSED, message=str(e))
    except DBBusyError:
        return tool_error(code=ErrorCode.DB_BUSY, message="Database is busy.")

    return tool_ok(
        text=result["result_message"],
        structured={
            "poll_id": poll_id,
            "topic_id": result["topic_id"],
            "question": result["question"],
            "status": "closed",
            "tally": result["tally"],
            "total_votes": result["total_votes"],
            "verdict": result["verdict"],
            "threshold": result["threshold"],
            "result_message": result["result_message"],
        },
    )


@mcp.tool(description="Get status and current tally for a poll.")
def poll_status(poll_id: str) -> Annotated[CallToolResult, PollStatusOutput]:
    """Get status and current tally for a poll."""
    poll_data = db.poll_get(poll_id=poll_id)
    if poll_data is None:
        return tool_error(code=ErrorCode.POLL_NOT_FOUND, message=f"Poll '{poll_id}' not found.")

    options = poll_data.get("options") or []
    votes = poll_data.get("votes") or []
    tally = {opt: 0 for opt in options}
    voters = {}
    for voter, choice, _ in votes:
        tally[choice] = tally.get(choice, 0) + 1
        voters[voter] = choice

    total_votes = len(votes)
    lines = [
        f"Poll: id={poll_id} status={poll_data['status']} threshold={poll_data['threshold']}",
        f"Question: {poll_data['question']}",
        f"Tally ({total_votes} votes): " + " / ".join(f"{opt} {tally.get(opt, 0)}" for opt in options),
    ]

    return tool_ok(
        text="\n".join(lines),
        structured={
            "poll_id": poll_id,
            "topic_id": poll_data["topic_id"],
            "question": poll_data["question"],
            "options": options,
            "threshold": poll_data["threshold"],
            "status": poll_data["status"],
            "created_by": poll_data["created_by"],
            "created_at": poll_data["created_at"],
            "closed_at": poll_data["closed_at"],
            "tally": tally,
            "total_votes": total_votes,
            "votes": voters,
        },
    )


def main(
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    import os

    from agent_bus.embedding_worker import start_background_embedding_worker

    start_background_embedding_worker(db)

    selected_transport = (
        transport
        or env_str(
            "AGENT_BUS_TRANSPORT", default="streamable-http" if os.environ.get("PORT") else "stdio"
        )
    ).lower()

    if selected_transport in ("streamable-http", "http", "sse"):
        # Trust boundary: standalone HTTP MCP runs without TokenAuthMiddleware, so
        # it must not be reachable. Use `agent-bus serve` (FastAPI + token auth).
        import sys

        print(
            "Refusing to start a standalone unauthenticated HTTP MCP server. "
            "Run `agent-bus serve` instead (token-authenticated HTTP deployment). "
            "stdio transport needs no auth.",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp.run(transport="stdio")
