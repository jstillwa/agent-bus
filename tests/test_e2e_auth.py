"""End-to-end: real token row -> real ASGI request -> MCP tool over HTTP.

Injecting identity into scope state (test_ownership.py) can miss plumbing
regressions; this drives the full middleware -> MCP tool path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import agent_bus.peer_server as peer_server
from agent_bus.db import AgentBusDB
from agent_bus.tokens import TokenStore
from agent_bus.web import server as web_server

ISS = "https://okta.example"


@pytest.mark.anyio
async def test_mcp_over_http_enforces_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bus_db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    monkeypatch.setattr(peer_server, "db", bus_db)
    web_server.init_db(str(tmp_path / "bus.sqlite"))
    store = TokenStore(path=tmp_path / "tokens.sqlite")
    monkeypatch.setattr("agent_bus.tokens.get_token_store", lambda: store)

    # run_server() mounts these; tests must mount them explicitly (once).
    if not any(getattr(route, "path", "") == "/mcp" for route in web_server.app.router.routes):
        web_server.setup_mcp_routes()

    _, alice_raw = store.mint(iss=ISS, sub="alice")
    _, bob_raw = store.mint(iss=ISS, sub="bob")

    # Run the MCP session manager in THIS loop (TestClient's portal deadlocks
    # with the ASGI transport in the same test). The manager is single-run and
    # shared across tests (test_admin's TestClient lifespan may have started it);
    # reset its single-run state for isolation.
    import asyncio

    mcp_server = peer_server.mcp
    mcp_server.streamable_http_app()  # ensure lazy session manager exists
    manager = mcp_server.session_manager
    manager._has_started = False  # type: ignore[attr-defined]
    manager._run_lock = asyncio.Lock()  # type: ignore[attr-defined]
    async with manager.run():

        async def call_tools(raw: str, calls: list[tuple[str, dict]]):
            transport = httpx.ASGITransport(app=web_server.app)

            def factory(**kwargs: Any) -> httpx.AsyncClient:
                kwargs["headers"] = {
                    **(kwargs.get("headers") or {}),
                    "Authorization": f"Bearer {raw}",
                }
                kwargs.pop("auth", None)
                return httpx.AsyncClient(transport=transport, **kwargs)

            async with (
                streamablehttp_client("http://testserver/mcp", httpx_client_factory=factory) as (
                    read,
                    write,
                    _get_session_id,
                ),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                results = []
                for name, args in calls:
                    results.append(await session.call_tool(name, args))
                return results

        # Alice creates a topic and tries to forge ownership via metadata.
        (created,) = await call_tools(
            alice_raw,
            [
                (
                    "topic_create",
                    {"name": "secret-project", "metadata": {"_owner": "forged"}, "mode": "new"},
                ),
            ],
        )
        assert not created.isError
        topic_id = created.structuredContent["topic_id"]
        topic = bus_db.get_topic(topic_id=topic_id)
        assert topic.metadata["_owner"] == f"{ISS}|alice"

        # Bob, over his own token: cannot see, join, or list Alice's topic.
        bob_results = await call_tools(
            bob_raw,
            [
                ("topic_join", {"agent_name": "bob-agent", "topic_id": topic_id}),
                ("topic_resolve", {"name": "secret-project"}),
                ("topic_list", {"status": "open"}),
                ("messages_search", {"query": "anything", "topic_id": topic_id, "mode": "fts"}),
            ],
        )
        join_err, resolve_err, listed, searched = bob_results
        assert join_err.isError and join_err.structuredContent["error"]["code"] == "TOPIC_NOT_FOUND"
        assert (
            resolve_err.isError
            and resolve_err.structuredContent["error"]["code"] == "TOPIC_NOT_FOUND"
        )
        assert listed.structuredContent["topics"] == []
        assert searched.isError and searched.structuredContent["error"]["code"] == "TOPIC_NOT_FOUND"

        # Unauthenticated request: middleware rejects before any tool runs.
        http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=web_server.app))
        try:
            res = await http_client.post(
                "http://testserver/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
            )
            assert res.status_code == 401
            assert res.headers["www-authenticate"] == "Bearer"
        finally:
            await http_client.aclose()

        # A wrong token value: rejected.
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_server.app),
            headers={"Authorization": "Bearer ab_wrong"},
        )
        try:
            res = await http_client.post(
                "http://testserver/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
            )
            assert res.status_code == 401
        finally:
            await http_client.aclose()
