import os

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _bin(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", ".venv", "bin", name)
    return path if os.path.exists(path) else name


@pytest.mark.anyio
async def test_regression_token_leak_and_phantom_name(tmp_path):
    db_path = str(tmp_path / "bus.sqlite")
    env = {**os.environ, "AGENT_BUS_DB": db_path, "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0"}
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (a_read, a_write),
        ClientSession(a_read, a_write) as agent_a,
    ):
        await agent_a.initialize()

        topic_res = await agent_a.call_tool("topic_create", {"name": "repro-topic", "mode": "new"})
        topic_id = topic_res.structuredContent["topic_id"]

        joined_1 = await agent_a.call_tool(
            "topic_join", {"topic_id": topic_id, "agent_name": "victim"}
        )
        assert joined_1.isError is False
        token_1 = joined_1.structuredContent["reclaim_token"]

        # 1. Phantom name rejection (CASE1)
        phantom_sync = await agent_a.call_tool(
            "sync", {"topic_id": topic_id, "agent_name": "never-joined", "wait_seconds": 0}
        )
        assert phantom_sync.isError is True
        if phantom_sync.structuredContent:
            assert phantom_sync.structuredContent["error"]["code"] == "AGENT_NOT_JOINED"
        else:
            assert "Not joined to topic" in phantom_sync.content[0].text

    # Second session tries to hijack
    async with (
        stdio_client(peer_server) as (b_read, b_write),
        ClientSession(b_read, b_write) as agent_b,
    ):
        await agent_b.initialize()

        # 2. Token leak / unauthorized rejoin rejection
        joined_2 = await agent_b.call_tool(
            "topic_join",
            {"topic_id": topic_id, "agent_name": "victim", "reclaim_token": "wrong-token"},
        )
        assert joined_2.isError is True
        if joined_2.structuredContent:
            assert joined_2.structuredContent["error"]["code"] == "AGENT_NAME_IN_USE"
        else:
            assert "already reserved" in joined_2.content[0].text

        assert token_1 not in str(joined_2.content)
        assert token_1 not in str(joined_2.structuredContent)
