from __future__ import annotations

from pathlib import Path

import pytest

import agent_bus.peer_server as peer_server
from agent_bus.auth import AuthIdentity
from agent_bus.db import AgentBusDB

ISS = "https://okta.example"

ALICE = AuthIdentity(iss=ISS, sub="alice", admin=False, browser=False, token_id="tok_a")
BOB = AuthIdentity(iss=ISS, sub="bob", admin=False, browser=False, token_id="tok_b")
ADMIN = AuthIdentity(iss=ISS, sub="ops", admin=True, browser=False, token_id="tok_x")


def acting(monkeypatch: pytest.MonkeyPatch, identity: AuthIdentity | None) -> None:
    monkeypatch.setattr(peer_server, "_current_auth", lambda: identity)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentBusDB:
    bus_db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    monkeypatch.setattr(peer_server, "db", bus_db)
    return bus_db


def err_code(result) -> str:
    return result.structuredContent["error"]["code"]


def post(db: AgentBusDB, topic_id: str, text: str, agent: str = "a") -> None:
    import asyncio

    result = asyncio.run(
        peer_server.sync(
            topic_id,
            agent_name=agent,
            outbox=[{"content_markdown": text}],
            wait_seconds=0,
        )
    )
    assert not result.isError, result.structuredContent


# --- topic_create -----------------------------------------------------------


def test_create_stamps_owner_and_strips_client_keys(
    db: AgentBusDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    acting(monkeypatch, ALICE)
    result = peer_server.topic_create(
        name="t1", metadata={"_owner": "forged", "custom": 1}, mode="new"
    )
    assert not result.isError
    topic = db.get_topic(topic_id=result.structuredContent["topic_id"])
    assert topic.metadata["_owner"] == f"{ISS}|alice"
    assert topic.metadata["custom"] == 1


def test_stdio_create_leaves_metadata_alone(
    db: AgentBusDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    acting(monkeypatch, None)
    result = peer_server.topic_create(name="t1", metadata={"custom": 1}, mode="new")
    assert not result.isError
    topic = db.get_topic(topic_id=result.structuredContent["topic_id"])
    assert topic.metadata == {"custom": 1}  # unowned: visible to admins only


def test_reuse_scopes_to_owned_topics(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    alice_id = peer_server.topic_create(name="dup", mode="new").structuredContent["topic_id"]

    # Bob reusing the same name gets his OWN new topic, not Alice's.
    acting(monkeypatch, BOB)
    bob_id = peer_server.topic_create(name="dup", mode="reuse").structuredContent["topic_id"]
    assert bob_id != alice_id

    # Bob reusing again resolves to his own topic.
    bob_again = peer_server.topic_create(name="dup", mode="reuse")
    assert bob_again.structuredContent["topic_id"] == bob_id


def test_admin_reuse_keeps_global_semantics(
    db: AgentBusDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    acting(monkeypatch, ALICE)
    alice_id = peer_server.topic_create(name="dup", mode="new").structuredContent["topic_id"]
    acting(monkeypatch, ADMIN)
    admin_reuse = peer_server.topic_create(name="dup", mode="reuse")
    assert admin_reuse.structuredContent["topic_id"] == alice_id


# --- visibility: foreign = not found ----------------------------------------


def test_foreign_topic_id_is_not_found(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    alice_id = peer_server.topic_create(name="t1", mode="new").structuredContent["topic_id"]

    acting(monkeypatch, BOB)
    joined = peer_server.topic_join(agent_name="bob-agent", topic_id=alice_id)
    assert joined.isError and err_code(joined) == "TOPIC_NOT_FOUND"

    resolved = peer_server.topic_resolve(name="t1")
    assert resolved.isError and err_code(resolved) == "TOPIC_NOT_FOUND"

    presence = peer_server.topic_presence(topic_id=alice_id)
    assert presence.isError and err_code(presence) == "TOPIC_NOT_FOUND"

    cursor = peer_server.cursor_reset(topic_id=alice_id, agent_name="bob-agent")
    assert cursor.isError and err_code(cursor) == "TOPIC_NOT_FOUND"

    closed = peer_server.topic_close(topic_id=alice_id)
    assert closed.isError and err_code(closed) == "TOPIC_NOT_FOUND"

    searched = peer_server.messages_search("anything", topic_id=alice_id)
    assert searched.isError and err_code(searched) == "TOPIC_NOT_FOUND"


def test_owned_topic_operations_pass(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    topic_id = peer_server.topic_create(name="t1", mode="new").structuredContent["topic_id"]
    assert not peer_server.topic_join(agent_name="alice-agent", topic_id=topic_id).isError
    assert not peer_server.topic_close(topic_id=topic_id).isError


def test_foreign_join_by_name_is_not_found(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    peer_server.topic_create(name="shared-name", mode="new")
    acting(monkeypatch, BOB)
    joined = peer_server.topic_join(agent_name="bob-agent", name="shared-name")
    assert joined.isError and err_code(joined) == "TOPIC_NOT_FOUND"
    resolved = peer_server.topic_resolve(name="shared-name", allow_closed=True)
    assert resolved.isError and err_code(resolved) == "TOPIC_NOT_FOUND"


def test_sync_foreign_topic_is_not_found(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    acting(monkeypatch, ALICE)
    topic_id = peer_server.topic_create(name="t1", mode="new").structuredContent["topic_id"]
    acting(monkeypatch, BOB)
    result = asyncio.run(
        peer_server.sync(topic_id, agent_name="bob-agent", outbox=[{"content_markdown": "hi"}])
    )
    assert result.isError and err_code(result) == "TOPIC_NOT_FOUND"


# --- topic_list / search filtering ------------------------------------------


def test_topic_list_scoped(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    for i in range(3):
        peer_server.topic_create(name=f"alice-{i}", mode="new")
    acting(monkeypatch, BOB)
    for i in range(2):
        peer_server.topic_create(name=f"bob-{i}", mode="new")

    bob_list = peer_server.topic_list(status="open")
    names = [t["name"] for t in bob_list.structuredContent["topics"]]
    assert len(names) == 2 and all(n.startswith("bob-") for n in names)


def test_topic_list_starvation_not_filtered_to_empty(
    db: AgentBusDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    acting(monkeypatch, ALICE)
    for i in range(50):
        peer_server.topic_create(name=f"alice-{i}", mode="new")
    acting(monkeypatch, BOB)
    for i in range(5):
        peer_server.topic_create(name=f"bob-{i}", mode="new")
    bob_list = peer_server.topic_list(status="open")
    assert len(bob_list.structuredContent["topics"]) == 5


def test_global_search_filtered_to_owned(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    alice_id = peer_server.topic_create(name="alice-topic", mode="new").structuredContent[
        "topic_id"
    ]
    post(db, alice_id, "unique needle alpha")
    acting(monkeypatch, BOB)
    bob_id = peer_server.topic_create(name="bob-topic", mode="new").structuredContent["topic_id"]
    post(db, bob_id, "unique needle beta")

    # Over-fetch must surface Bob's own match even when Alice's matches dominate.
    results = peer_server.messages_search("needle", mode="fts", limit=5)
    assert not results.isError
    topic_ids = {r["topic_id"] for r in results.structuredContent["results"]}
    assert topic_ids == {bob_id}


def test_admin_sees_everything(db: AgentBusDB, monkeypatch: pytest.MonkeyPatch) -> None:
    acting(monkeypatch, ALICE)
    alice_id = peer_server.topic_create(name="alice-topic", mode="new").structuredContent[
        "topic_id"
    ]
    acting(monkeypatch, None)
    stdio_id = peer_server.topic_create(name="unowned", mode="new").structuredContent["topic_id"]

    acting(monkeypatch, ADMIN)
    listing = peer_server.topic_list(status="open")
    ids = {t["topic_id"] for t in listing.structuredContent["topics"]}
    assert {alice_id, stdio_id} <= ids

    joined = peer_server.topic_join(agent_name="ops-agent", topic_id=alice_id)
    assert not joined.isError
