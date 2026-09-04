"""Topic ownership scoping shared by the MCP tools and the web API.

Ownership is stored as the reserved `metadata["_owner"]` key, stamped by the
server at topic creation: `"<iss>|<sub>"`. Client-supplied `_`-prefixed
metadata keys are stripped on every write path — the `_` namespace is
server-managed (documented in spec.md).

Visibility semantics (non-admin): foreign topics are *invisible*, not
forbidden — access to a topic owned by someone else behaves exactly like a
missing topic (TOPIC_NOT_FOUND / 404). Admins see everything, including
unowned topics (created via stdio), for cleanup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_bus.db import TopicNotFoundError

if TYPE_CHECKING:
    from agent_bus.auth import AuthIdentity
    from agent_bus.db import AgentBusDB
    from agent_bus.models import Topic

OWNER_KEY = "_owner"


def owner_key(auth: AuthIdentity) -> str:
    return f"{auth.iss}|{auth.sub}"


def is_visible(auth: AuthIdentity, topic: Topic) -> bool:
    """Admin sees everything; otherwise only topics the user created."""
    if auth.admin:
        return True
    metadata = topic.metadata or {}
    return metadata.get(OWNER_KEY) == owner_key(auth)


def sanitize_topic_metadata(
    metadata: dict[str, Any] | None, auth: AuthIdentity | None
) -> dict[str, Any]:
    """Strip client `_`-prefixed (server-managed) keys; stamp `_owner` for HTTP callers."""
    out = {k: v for k, v in (metadata or {}).items() if not k.startswith("_")}
    if auth is not None:
        out[OWNER_KEY] = owner_key(auth)
    return out


def visible_topics(auth: AuthIdentity | None, topics: list[Topic]) -> list[Topic]:
    """stdio callers (auth=None) see everything, matching local-file trust."""
    if auth is None or auth.admin:
        return topics
    return [t for t in topics if is_visible(auth, t)]


def resolve_owned_topic(
    db: AgentBusDB, auth: AuthIdentity, *, name: str, allow_closed: bool
) -> Topic:
    """Name resolution scoped to the caller's topics (mirrors core topic_resolve ordering)."""
    matches = [t for t in db.topic_list(status="all") if is_visible(auth, t) and t.name == name]
    open_matches = [t for t in matches if t.status == "open"]
    if open_matches:
        return max(open_matches, key=lambda t: t.created_at)
    if allow_closed and matches:
        return max(matches, key=lambda t: t.created_at)
    raise TopicNotFoundError(name)


def is_visible_topic_id(db: AgentBusDB, auth: AuthIdentity, topic_id: str) -> bool:
    """False for missing *and* foreign topics — callers report TOPIC_NOT_FOUND for both."""
    try:
        topic = db.get_topic(topic_id=topic_id)
    except TopicNotFoundError:
        return False
    return is_visible(auth, topic)
