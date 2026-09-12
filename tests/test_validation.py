from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import ValidationError

from agent_bus.tool_schemas import PingOutput


def _bin(name: str) -> str:
    return str(Path(sys.executable).with_name(name))


def test_ping_output_requires_package_version() -> None:
    with pytest.raises(ValidationError, match="Missing required field: package_version"):
        PingOutput(ok=True, spec_version="v6.3")


@pytest.mark.anyio
async def test_sync_max_message_length_validation(tmp_path):
    env = {
        **os.environ,
        "AGENT_BUS_DB": str(tmp_path / "bus.sqlite"),
        "AGENT_BUS_MAX_MESSAGE_CHARS": "3",
        "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0",
    }
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (s_read, s_write),
        ClientSession(s_read, s_write) as peer,
    ):
        await peer.initialize()
        created = await peer.call_tool("topic_create", {"name": "pink"})
        topic_id = created.structuredContent["topic_id"]
        await peer.call_tool("topic_join", {"agent_name": "a", "topic_id": topic_id})

        res = await peer.call_tool(
            "sync",
            {
                "topic_id": topic_id,
                "agent_name": "a",
                "outbox": [{"content_markdown": "hello", "message_type": "message"}],
                "wait_seconds": 0,
            },
        )
        assert res.isError is True
        assert res.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
        assert "exceeds max length" in res.structuredContent["error"]["message"]


@pytest.mark.anyio
async def test_invalid_sync_max_items_env_is_a_tool_error_not_startup_failure(tmp_path):
    env = {
        **os.environ,
        "AGENT_BUS_DB": str(tmp_path / "bus.sqlite"),
        "AGENT_BUS_MAX_SYNC_ITEMS": "bogus",
        "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0",
    }
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (s_read, s_write),
        ClientSession(s_read, s_write) as peer,
    ):
        await peer.initialize()

        ping = await peer.call_tool("ping", {})
        assert ping.isError is False

        created = await peer.call_tool("topic_create", {"name": "pink"})
        assert created.isError is False
        topic_id = created.structuredContent["topic_id"]

        joined = await peer.call_tool("topic_join", {"agent_name": "a", "topic_id": topic_id})
        assert joined.isError is False

        res = await peer.call_tool(
            "sync", {"topic_id": topic_id, "agent_name": "a", "wait_seconds": 0}
        )
        assert res.isError is True
        assert res.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
        assert (
            "AGENT_BUS_MAX_SYNC_ITEMS must be an int" in res.structuredContent["error"]["message"]
        )


@pytest.mark.anyio
async def test_topic_join_rejects_blank_reclaim_token(tmp_path):
    env = {
        **os.environ,
        "AGENT_BUS_DB": str(tmp_path / "bus.sqlite"),
        "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0",
    }
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (s_read, s_write),
        ClientSession(s_read, s_write) as peer,
    ):
        await peer.initialize()
        created = await peer.call_tool("topic_create", {"name": "pink"})
        topic_id = created.structuredContent["topic_id"]

        joined = await peer.call_tool("topic_join", {"agent_name": "a", "topic_id": topic_id})
        assert joined.isError is False

        res = await peer.call_tool(
            "topic_join",
            {"agent_name": "a", "topic_id": topic_id, "reclaim_token": "   "},
        )
        assert res.isError is True
        assert res.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
        assert (
            res.structuredContent["error"]["message"] == "reclaim_token must be a non-empty string"
        )


@pytest.mark.anyio
async def test_sync_and_cursor_reset_require_agent_name(tmp_path):
    env = {
        **os.environ,
        "AGENT_BUS_DB": str(tmp_path / "bus.sqlite"),
        "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0",
    }
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (s_read, s_write),
        ClientSession(s_read, s_write) as peer,
    ):
        await peer.initialize()
        created = await peer.call_tool("topic_create", {"name": "req-name"})
        topic_id = created.structuredContent["topic_id"]

        # sync without agent_name must be rejected by schema validation
        sync_res = await peer.call_tool("sync", {"topic_id": topic_id, "wait_seconds": 0})
        assert sync_res.isError is True

        # cursor_reset without agent_name must be rejected by schema validation
        cursor_res = await peer.call_tool("cursor_reset", {"topic_id": topic_id, "last_seq": 0})
        assert cursor_res.isError is True


@pytest.mark.anyio
async def test_topic_join_does_not_leak_reclaim_token(tmp_path):
    env = {
        **os.environ,
        "AGENT_BUS_DB": str(tmp_path / "bus.sqlite"),
        "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0",
    }
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (s_read, s_write),
        ClientSession(s_read, s_write) as peer,
    ):
        await peer.initialize()
        created = await peer.call_tool("topic_create", {"name": "leak-test"})
        topic_id = created.structuredContent["topic_id"]

        first_join = await peer.call_tool(
            "topic_join", {"agent_name": "victim", "topic_id": topic_id}
        )
        assert first_join.isError is False
        real_token = first_join.structuredContent["reclaim_token"]

        # Attempt to rejoin as victim with wrong token: must fail, must NOT disclose real_token
        bad_join = await peer.call_tool(
            "topic_join",
            {"agent_name": "victim", "topic_id": topic_id, "reclaim_token": "wrong-token"},
        )
        assert bad_join.isError is True
        assert bad_join.structuredContent["error"]["code"] == "AGENT_NAME_IN_USE"
        assert bad_join.structuredContent.get("reclaim_token") != real_token
        assert real_token not in getattr(bad_join.content[0], "text", "")
