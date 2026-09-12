from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_bus.common import ErrorCode
from agent_bus.db import (
    AgentBusDB,
    AgentNotJoinedError,
    MutedError,
    PollClosedError,
    PollNotFoundError,
)


def _bin(name: str) -> str:
    return str(Path(sys.executable).with_name(name))


def test_schema_migration_v6_to_v7(tmp_path):
    db_path = str(tmp_path / "legacy_v6.sqlite")

    # Seed a simulated v6 database
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '6'), ('topics_version', '0')"
        )
        conn.execute(
            """
            CREATE TABLE topics (
              topic_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              created_at REAL NOT NULL,
              status TEXT NOT NULL,
              closed_at REAL NULL,
              close_reason TEXT NULL,
              metadata_json TEXT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE topic_seq (
              topic_id TEXT PRIMARY KEY,
              next_seq INTEGER NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE messages (
              message_id TEXT PRIMARY KEY,
              topic_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              sender TEXT NOT NULL,
              message_type TEXT NOT NULL,
              reply_to TEXT NULL,
              content_markdown TEXT NOT NULL,
              metadata_json TEXT NULL,
              client_message_id TEXT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE cursors (
              topic_id TEXT NOT NULL,
              agent_name TEXT NOT NULL,
              last_seq INTEGER NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY(topic_id, agent_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE agent_name_reservations (
              topic_id TEXT NOT NULL,
              agent_name TEXT NOT NULL,
              reclaim_token TEXT NOT NULL,
              created_at REAL NOT NULL,
              last_claimed_at REAL NOT NULL,
              PRIMARY KEY(topic_id, agent_name)
            )
            """
        )
        conn.execute(
            "INSERT INTO topics VALUES ('t1', 'old-topic', 100.0, 'open', NULL, NULL, '{\"chair\": \"alice\"}')"
        )
        conn.execute("INSERT INTO topic_seq VALUES ('t1', 1, 100.0)")
        conn.execute(
            "INSERT INTO messages VALUES ('m1', 't1', 0, 'alice', 'message', NULL, 'prior history', NULL, NULL, 100.0)"
        )

    # Opening with AgentBusDB must migrate to v7 without error
    db = AgentBusDB(path=db_path)
    topic = db.get_topic(topic_id="t1")
    assert topic.name == "old-topic"
    assert topic.metadata == {"chair": "alice"}

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        assert version == "7"

        # Check that polls and poll_votes tables now exist
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "polls" in tables
        assert "poll_votes" in tables


def test_db_mute_enforcement_in_sync_once(tmp_path):
    db_path = str(tmp_path / "mute_test.sqlite")
    db = AgentBusDB(path=db_path)

    topic = db.topic_create(
        name="chaired-discussion",
        metadata={"chair": "pi-chair", "muted": ["noisy-agent"]},
        mode="new",
    )
    topic_id = topic.topic_id

    # Join both peers
    db.reserve_agent_name(topic_id=topic_id, agent_name="pi-chair")
    db.reserve_agent_name(topic_id=topic_id, agent_name="noisy-agent")

    # Chair can post messages
    sent, _, _, _ = db.sync_once(
        topic_id=topic_id,
        agent_name="pi-chair",
        outbox=[{"content_markdown": "Order in the court!", "message_type": "message"}],
        max_items=10,
        include_self=False,
        auto_advance=True,
        ack_through=None,
    )
    assert len(sent) == 1

    # Muted agent can read (empty outbox)
    _, received, _cursor, _ = db.sync_once(
        topic_id=topic_id,
        agent_name="noisy-agent",
        outbox=[],
        max_items=10,
        include_self=False,
        auto_advance=True,
        ack_through=None,
    )
    assert len(received) == 1
    assert received[0].content_markdown == "Order in the court!"

    # Muted agent cannot post (MutedError)
    with pytest.raises(MutedError) as excinfo:
        db.sync_once(
            topic_id=topic_id,
            agent_name="noisy-agent",
            outbox=[{"content_markdown": "I speak anyway!", "message_type": "message"}],
            max_items=10,
            include_self=False,
            auto_advance=True,
            ack_through=None,
        )
    assert "muted" in str(excinfo.value)


def test_db_poll_lifecycle_and_roberts_rules(tmp_path):
    db_path = str(tmp_path / "poll_test.sqlite")
    db = AgentBusDB(path=db_path)

    topic = db.topic_create(
        name="deliberation",
        metadata={"chair": "pi-chair"},
        mode="new",
    )
    topic_id = topic.topic_id

    # Join voters
    db.reserve_agent_name(topic_id=topic_id, agent_name="pi-chair")
    db.reserve_agent_name(topic_id=topic_id, agent_name="voter-1")
    db.reserve_agent_name(topic_id=topic_id, agent_name="voter-2")
    db.reserve_agent_name(topic_id=topic_id, agent_name="voter-3")

    # Open poll
    poll = db.poll_create(
        topic_id=topic_id,
        question="Should we merge?",
        options=["yay", "nay", "abstain"],
        threshold="majority",
        created_by="pi-chair",
    )
    poll_id = poll["poll_id"]
    assert poll["status"] == "open"
    assert poll["options"] == ["yay", "nay", "abstain"]

    # Unjoined voter fails
    with pytest.raises(AgentNotJoinedError):
        db.poll_vote(poll_id=poll_id, caller="outsider", choice="yay")

    # Invalid choice fails
    with pytest.raises(ValueError):
        db.poll_vote(poll_id=poll_id, caller="voter-1", choice="maybe")

    # Unknown poll raises PollNotFoundError
    with pytest.raises(PollNotFoundError):
        db.poll_vote(poll_id="non-existent-poll", caller="voter-1", choice="yay")

    # Cast valid votes
    v1 = db.poll_vote(poll_id=poll_id, caller="voter-1", choice="yay")
    assert v1["choice"] == "yay"
    v2 = db.poll_vote(poll_id=poll_id, caller="voter-2", choice="nay")
    assert v2["choice"] == "nay"
    v3 = db.poll_vote(poll_id=poll_id, caller="voter-3", choice="abstain")
    assert v3["choice"] == "abstain"

    # voter-2 changes vote to yay (upsert)
    v2_updated = db.poll_vote(poll_id=poll_id, caller="voter-2", choice="yay")
    assert v2_updated["choice"] == "yay"

    # Inspect status before close
    status = db.poll_get(poll_id=poll_id)
    assert status["status"] == "open"
    assert len(status["votes"]) == 3

    # Close poll: 2 yay, 0 nay, 1 abstain -> majority (>50% of 2 non-abstain votes = 2/2 = 100%) -> ADOPTED
    result = db.poll_close(poll_id=poll_id)
    assert result["status"] == "closed"
    assert result["verdict"] == "ADOPTED"
    assert result["total_votes"] == 3
    assert result["tally"] == {"yay": 2, "nay": 0, "abstain": 1}
    assert "ADOPTED (majority)" in result["result_message"]
    assert "POLL CLOSED: Should we merge?" in result["result_message"]

    # Vote after close raises PollClosedError
    with pytest.raises(PollClosedError):
        db.poll_vote(poll_id=poll_id, caller="voter-1", choice="nay")

    # Verification: result message was recorded into topic
    _, received, _, _ = db.sync_once(
        topic_id=topic_id,
        agent_name="voter-1",
        outbox=[],
        max_items=10,
        include_self=True,
        auto_advance=True,
        ack_through=None,
    )
    assert any("POLL CLOSED: Should we merge?" in m.content_markdown for m in received)


def test_db_poll_two_thirds_and_plurality(tmp_path):
    db_path = str(tmp_path / "thresholds.sqlite")
    db = AgentBusDB(path=db_path)
    topic = db.topic_create(name="t", metadata={"chair": "c"}, mode="new")
    tid = topic.topic_id
    for name in ["c", "p1", "p2", "p3"]:
        db.reserve_agent_name(topic_id=tid, agent_name=name)

    # Two-thirds test: 2 yay vs 1 nay (66.7% yay) -> ADOPTED
    poll_23 = db.poll_create(
        topic_id=tid,
        question="Amend constitution?",
        options=["yay", "nay"],
        threshold="two-thirds",
        created_by="c",
    )
    db.poll_vote(poll_id=poll_23["poll_id"], caller="p1", choice="yay")
    db.poll_vote(poll_id=poll_23["poll_id"], caller="p2", choice="yay")
    db.poll_vote(poll_id=poll_23["poll_id"], caller="p3", choice="nay")
    res_23 = db.poll_close(poll_id=poll_23["poll_id"])
    assert res_23["verdict"] == "ADOPTED"

    # Multi-option plurality test
    poll_multi = db.poll_create(
        topic_id=tid,
        question="Color?",
        options=["Red", "Green", "Blue"],
        threshold="plurality",
        created_by="c",
    )
    db.poll_vote(poll_id=poll_multi["poll_id"], caller="p1", choice="Blue")
    db.poll_vote(poll_id=poll_multi["poll_id"], caller="p2", choice="Blue")
    db.poll_vote(poll_id=poll_multi["poll_id"], caller="p3", choice="Red")
    res_multi = db.poll_close(poll_id=poll_multi["poll_id"])
    assert res_multi["verdict"] == "ADOPTED"
    assert res_multi["tally"] == {"Red": 1, "Green": 0, "Blue": 2}


@pytest.mark.anyio
async def test_mcp_tools_chaired_topics_and_polls(tmp_path):
    db_path = str(tmp_path / "bus.sqlite")
    env = {**os.environ, "AGENT_BUS_DB": db_path, "AGENT_BUS_EMBEDDINGS_AUTOINDEX": "0"}
    peer_server = StdioServerParameters(command=_bin("agent-bus"), env=env)

    async with (
        stdio_client(peer_server) as (c_read, c_write),
        ClientSession(c_read, c_write) as chair_client,
    ):
        await chair_client.initialize()

        # Check tool schemas
        tools = await chair_client.list_tools()
        tool_names = {t.name for t in tools.tools}
        assert "topic_update" in tool_names
        assert "chair_mute" in tool_names
        assert "chair_unmute" in tool_names
        assert "poll_open" in tool_names
        assert "poll_vote" in tool_names
        assert "poll_close" in tool_names
        assert "poll_status" in tool_names

        # Create chaired topic
        created = await chair_client.call_tool(
            "topic_create",
            {"name": "court-room", "metadata": {"chair": "chief-justice"}},
        )
        assert created.isError is False
        topic_id = created.structuredContent["topic_id"]

        # Chair joins
        joined_chair = await chair_client.call_tool(
            "topic_join",
            {"agent_name": "chief-justice", "topic_id": topic_id},
        )
        assert joined_chair.isError is False

        # Peer client joins
        async with (
            stdio_client(peer_server) as (p_read, p_write),
            ClientSession(p_read, p_write) as peer_client,
        ):
            await peer_client.initialize()
            joined_peer = await peer_client.call_tool(
                "topic_join",
                {"agent_name": "associate-justice", "topic_id": topic_id},
            )
            assert joined_peer.isError is False

            # Peer posts message initially
            post1 = await peer_client.call_tool(
                "sync",
                {
                    "topic_id": topic_id,
                    "agent_name": "associate-justice",
                    "outbox": [
                        {"content_markdown": "I have an argument", "message_type": "message"}
                    ],
                    "wait_seconds": 0,
                },
            )
            assert post1.isError is False
            assert post1.structuredContent["topic_metadata"]["chair"] == "chief-justice"

            # Non-chair cannot mute
            bad_mute = await peer_client.call_tool(
                "chair_mute",
                {"topic_id": topic_id, "caller": "associate-justice", "target": "chief-justice"},
            )
            assert bad_mute.isError is True
            assert bad_mute.structuredContent["error"]["code"] == ErrorCode.NOT_CHAIR

            # Chair mutes associate-justice
            mute_res = await chair_client.call_tool(
                "chair_mute",
                {"topic_id": topic_id, "caller": "chief-justice", "target": "associate-justice"},
            )
            assert mute_res.isError is False
            assert mute_res.structuredContent["muted"] is True

            # Associate-justice attempts to post -> rejected with MUTED
            muted_post = await peer_client.call_tool(
                "sync",
                {
                    "topic_id": topic_id,
                    "agent_name": "associate-justice",
                    "outbox": [{"content_markdown": "Filibustering!", "message_type": "message"}],
                    "wait_seconds": 0,
                },
            )
            assert muted_post.isError is True
            assert muted_post.structuredContent["error"]["code"] == ErrorCode.MUTED

            # Associate-justice can still read
            read_res = await peer_client.call_tool(
                "sync",
                {
                    "topic_id": topic_id,
                    "agent_name": "associate-justice",
                    "wait_seconds": 0,
                },
            )
            assert read_res.isError is False
            assert read_res.structuredContent["topic_metadata"]["muted"] == ["associate-justice"]

            # Chair unmutes
            unmute_res = await chair_client.call_tool(
                "chair_unmute",
                {"topic_id": topic_id, "caller": "chief-justice", "target": "associate-justice"},
            )
            assert unmute_res.isError is False
            assert unmute_res.structuredContent["muted"] is False

            # Peer can post again
            post2 = await peer_client.call_tool(
                "sync",
                {
                    "topic_id": topic_id,
                    "agent_name": "associate-justice",
                    "outbox": [
                        {"content_markdown": "Back on the record.", "message_type": "message"}
                    ],
                    "wait_seconds": 0,
                },
            )
            assert post2.isError is False

            # Chair opens poll
            poll_res = await chair_client.call_tool(
                "poll_open",
                {
                    "topic_id": topic_id,
                    "caller": "chief-justice",
                    "question": "Adopt the resolution?",
                    "options": ["yay", "nay", "abstain"],
                    "threshold": "majority",
                },
            )
            assert poll_res.isError is False
            poll_id = poll_res.structuredContent["poll_id"]

            # Peer casts vote
            vote_res = await peer_client.call_tool(
                "poll_vote",
                {"poll_id": poll_id, "caller": "associate-justice", "choice": "yay"},
            )
            assert vote_res.isError is False

            # Check poll status
            status_res = await peer_client.call_tool("poll_status", {"poll_id": poll_id})
            assert status_res.isError is False
            assert status_res.structuredContent["tally"]["yay"] == 1
            assert status_res.structuredContent["votes"]["associate-justice"] == "yay"

            # Chair closes poll
            close_res = await chair_client.call_tool(
                "poll_close",
                {"poll_id": poll_id, "caller": "chief-justice"},
            )
            assert close_res.isError is False
            assert close_res.structuredContent["verdict"] == "ADOPTED"
            assert (
                "POLL CLOSED: Adopt the resolution?"
                in close_res.structuredContent["result_message"]
            )
