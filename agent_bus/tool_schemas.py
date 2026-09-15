from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, model_validator

SearchMode = Literal["hybrid", "fts", "semantic"]
TopicStatus = Literal["open", "closed"]
SyncStatus = Literal["ready", "timeout", "empty"]


class ToolErrorInfo(BaseModel):
    code: str
    message: str


class ToolWarningInfo(BaseModel):
    code: str
    message: str | None = None
    context: dict[str, Any] | None = None


class ToolOutputBase(BaseModel):
    model_config = ConfigDict(extra="allow")

    error: ToolErrorInfo | None = None
    warnings: list[ToolWarningInfo] | None = None

    required_on_success: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _validate_required_on_success(self) -> ToolOutputBase:
        if self.error is not None:
            return self
        for field in self.required_on_success:
            if getattr(self, field) is None:
                raise ValueError(f"Missing required field: {field}")
        return self


class CursorInfo(BaseModel):
    last_seq: int
    updated_at: float


class TopicInfo(BaseModel):
    topic_id: str
    name: str
    status: TopicStatus
    created_at: float
    closed_at: float | None
    close_reason: str | None
    metadata: dict[str, Any] | None


class MessageInfo(BaseModel):
    message_id: str
    topic_id: str
    seq: int
    sender: str
    message_type: str
    reply_to: str | None
    content_markdown: str
    metadata: dict[str, Any] | None
    client_message_id: str | None
    created_at: float


class SentMessageInfo(BaseModel):
    message: MessageInfo
    duplicate: bool


class SearchResultInfo(BaseModel):
    topic_id: str
    topic_name: str
    message_id: str
    seq: int
    sender: str
    message_type: str
    created_at: float
    snippet: str
    rank: float | None = None
    fts_rank: float | None = None
    semantic_score: float | None = None
    content_markdown: str | None = None


class PresencePeerInfo(BaseModel):
    agent_name: str
    last_seq: int
    updated_at: float
    age_seconds: float


class PingOutput(ToolOutputBase):
    required_on_success = ("ok", "spec_version", "package_version")

    ok: bool | None = None
    spec_version: str | None = None
    package_version: str | None = None


class TopicCreateOutput(ToolOutputBase):
    required_on_success = ("topic_id", "name", "status")

    topic_id: str | None = None
    name: str | None = None
    status: TopicStatus | None = None


class TopicListOutput(ToolOutputBase):
    required_on_success = ("topics",)

    topics: list[TopicInfo] | None = None


class TopicCloseOutput(ToolOutputBase):
    required_on_success = ("topic_id", "status", "closed_at")

    topic_id: str | None = None
    status: TopicStatus | None = None
    closed_at: float | None = None
    close_reason: str | None = None


class TopicReopenOutput(ToolOutputBase):
    required_on_success = ("topic_id", "status")

    topic_id: str | None = None
    status: TopicStatus | None = None
    reopened_now: bool | None = None


class SetRateLimitOutput(ToolOutputBase):
    required_on_success = ("topic_id", "rate_limit")

    topic_id: str | None = None
    rate_limit: float | None = None


class TopicResolveOutput(ToolOutputBase):
    required_on_success = ("topic_id", "name", "status")

    topic_id: str | None = None
    name: str | None = None
    status: TopicStatus | None = None


class TopicJoinOutput(ToolOutputBase):
    required_on_success = ("topic_id", "name", "status", "agent_name", "reclaim_token")

    topic_id: str | None = None
    name: str | None = None
    status: TopicStatus | None = None
    agent_name: str | None = None
    reclaim_token: str | None = None


class TopicPresenceOutput(ToolOutputBase):
    required_on_success = ("topic_id", "window_seconds", "limit", "now", "peers", "count")

    topic_id: str | None = None
    window_seconds: int | None = None
    limit: int | None = None
    now: float | None = None
    peers: list[PresencePeerInfo] | None = None
    count: int | None = None


class CursorResetOutput(ToolOutputBase):
    required_on_success = ("topic_id", "agent_name", "cursor")

    topic_id: str | None = None
    agent_name: str | None = None
    cursor: CursorInfo | None = None


class MessagesSearchOutput(ToolOutputBase):
    required_on_success = ("query", "mode", "include_content", "results", "count")

    query: str | None = None
    mode: SearchMode | None = None
    topic_id: str | None = None
    include_content: bool | None = None
    results: list[SearchResultInfo] | None = None
    count: int | None = None


class SyncOutput(ToolOutputBase):
    required_on_success = (
        "topic_id",
        "agent_name",
        "status",
        "cursor",
        "sent",
        "received",
        "received_count",
        "has_more",
    )

    topic_id: str | None = None
    agent_name: str | None = None
    status: SyncStatus | None = None
    cursor: CursorInfo | None = None
    sent: list[SentMessageInfo] | None = None
    received: list[MessageInfo] | None = None
    received_count: int | None = None
    has_more: bool | None = None
    topic_metadata: dict[str, Any] | None = None


class TopicUpdateOutput(ToolOutputBase):
    required_on_success = ("topic_id", "metadata")

    topic_id: str | None = None
    metadata: dict[str, Any] | None = None


class ChairMuteOutput(ToolOutputBase):
    required_on_success = ("topic_id", "target", "muted")

    topic_id: str | None = None
    target: str | None = None
    muted: bool | None = None


class ChairUnmuteOutput(ToolOutputBase):
    required_on_success = ("topic_id", "target", "muted")

    topic_id: str | None = None
    target: str | None = None
    muted: bool | None = None


class PollOpenOutput(ToolOutputBase):
    required_on_success = ("poll_id", "topic_id", "question", "options", "threshold", "status")

    poll_id: str | None = None
    topic_id: str | None = None
    question: str | None = None
    options: list[str] | None = None
    threshold: str | None = None
    status: str | None = None


class PollVoteOutput(ToolOutputBase):
    required_on_success = ("poll_id", "caller", "choice")

    poll_id: str | None = None
    caller: str | None = None
    choice: str | None = None


class PollCloseOutput(ToolOutputBase):
    required_on_success = (
        "poll_id",
        "topic_id",
        "question",
        "status",
        "tally",
        "total_votes",
        "verdict",
        "threshold",
        "result_message",
    )

    poll_id: str | None = None
    topic_id: str | None = None
    question: str | None = None
    status: str | None = None
    tally: dict[str, int] | None = None
    total_votes: int | None = None
    verdict: str | None = None
    threshold: str | None = None
    result_message: str | None = None


class PollStatusOutput(ToolOutputBase):
    required_on_success = (
        "poll_id",
        "topic_id",
        "question",
        "options",
        "threshold",
        "status",
        "tally",
        "total_votes",
    )

    poll_id: str | None = None
    topic_id: str | None = None
    question: str | None = None
    options: list[str] | None = None
    threshold: str | None = None
    status: str | None = None
    created_by: str | None = None
    created_at: float | None = None
    closed_at: float | None = None
    tally: dict[str, int] | None = None
    total_votes: int | None = None
    votes: dict[str, str] | None = None
