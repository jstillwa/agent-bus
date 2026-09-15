from __future__ import annotations

import pytest

from agent_bus.common import ErrorCode
from agent_bus.db import AgentBusDB, RateLimitedError


def _post(db: AgentBusDB, topic_id: str, agent: str, body: str):
    return db.sync_once(
        topic_id=topic_id,
        agent_name=agent,
        outbox=[{"content_markdown": body, "message_type": "message"}],
        max_items=0,
        include_self=False,
        auto_advance=True,
        ack_through=None,
    )


def _read(db: AgentBusDB, topic_id: str, agent: str):
    return db.sync_once(
        topic_id=topic_id,
        agent_name=agent,
        outbox=[],
        max_items=50,
        include_self=False,
        auto_advance=True,
        ack_through=None,
    )


def _make_topic(db: AgentBusDB, *, rate_limit: float | None = None, peers: int = 0):
    metadata = {} if rate_limit is None else {"rate_limit": rate_limit}
    topic = db.topic_create(name="rl", mode="new", metadata=metadata)
    for i in range(peers):
        # Joining is materialized by any sync; an empty read is enough.
        _read(db, topic.topic_id, f"peer-{i}")
    return topic


def test_rate_limit_absent_allows_unlimited_posting(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=None, peers=3)

    for i in range(5):
        _post(db, topic.topic_id, "talker", f"m{i}")


def test_rate_limit_zero_allows_unlimited_posting(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=0.0, peers=3)

    for i in range(5):
        _post(db, topic.topic_id, "talker", f"m{i}")


def test_rate_limit_half_blocks_until_half_of_peers_post(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=0.5, peers=4)
    tid = topic.topic_id

    # First post is exempt: a peer that has never spoken may open the round.
    _post(db, tid, "talker", "opening")

    # 0 of 4 peers have posted since -> blocked.
    with pytest.raises(RateLimitedError) as excinfo:
        _post(db, tid, "talker", "too soon")
    assert "0 of 4" in str(excinfo.value)

    _post(db, tid, "peer-0", "one")
    # 1 of 4 -> still below 0.5.
    with pytest.raises(RateLimitedError):
        _post(db, tid, "talker", "still too soon")

    _post(db, tid, "peer-1", "two")
    # 2 of 4 == 0.5 -> allowed.
    _post(db, tid, "talker", "my turn again")


def test_rate_limit_one_requires_every_peer_to_post(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=3)
    tid = topic.topic_id

    _post(db, tid, "talker", "opening")

    for i in range(3):
        with pytest.raises(RateLimitedError) as excinfo:
            _post(db, tid, "talker", f"premature-{i}")
        # Counting is distinct-by-sender, so repeat posts from one peer do not count twice.
        assert f"{i} of 3" in str(excinfo.value)
        _post(db, tid, f"peer-{i}", f"reply-{i}")

    # All three peers have now posted since the talker's last message.
    _post(db, tid, "talker", "allowed")


def test_rate_limit_one_does_not_deadlock_the_opening_round(tmp_path):
    """With rate_limit=1.0 nobody would ever move if first posts were gated."""
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=3)

    for i in range(3):
        _post(db, topic.topic_id, f"peer-{i}", f"opening-{i}")


def test_unique_poster_does_not_satisfy_the_quota_twice(tmp_path):
    """Repeat posts from one peer count once, so they cannot fill a multi-peer quota."""
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=3)
    tid = topic.topic_id

    _post(db, tid, "talker", "opening")
    # peer-0's first post is exempt; its second would need all 3 peers to have posted.
    _post(db, tid, "peer-0", "one")
    _post(db, tid, "peer-1", "two")

    # Only two distinct peers have posted, so talker still needs the third.
    with pytest.raises(RateLimitedError) as excinfo:
        _post(db, tid, "talker", "premature")
    assert "2 of 3" in str(excinfo.value)

    _post(db, tid, "peer-2", "three")
    _post(db, tid, "talker", "allowed")


def test_repeat_posts_from_one_peer_count_once_for_the_quota(tmp_path):
    """A peer posting repeatedly must not satisfy a quota meant for distinct peers."""
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=3)
    tid = topic.topic_id

    _post(db, tid, "talker", "opening")
    # peer-0 may post once (exempt); later attempts are gated but peer-0 has already
    # contributed its one distinct vote toward the talker's quota.
    _post(db, tid, "peer-0", "only contribution")

    with pytest.raises(RateLimitedError) as excinfo:
        _post(db, tid, "talker", "premature")
    assert "1 of 3" in str(excinfo.value), "one peer's post must count exactly once"


def test_rate_limit_applies_to_every_peer_not_just_one(tmp_path):
    """A 1.0 limit makes every peer yield to all others, not just one designated peer."""
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=2)
    tid = topic.topic_id

    _post(db, tid, "talker", "opening")
    _post(db, tid, "peer-0", "first is exempt")

    # peer-0 must yield to BOTH other peers (talker and peer-1), so one reply is not enough.
    _post(db, tid, "peer-1", "my turn")
    with pytest.raises(RateLimitedError) as excinfo:
        _post(db, tid, "peer-0", "needs the talker too")
    assert "1 of 2" in str(excinfo.value)

    _post(db, tid, "talker", "back to the talker")
    _post(db, tid, "peer-0", "now everybody has posted since my last message")


def test_rate_limited_peer_can_still_read_history(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=2)
    tid = topic.topic_id

    _post(db, tid, "talker", "opening")
    _post(db, tid, "peer-0", "reply")

    # talker has posted, so it is now gated: only 1 of 2 peers has replied.
    with pytest.raises(RateLimitedError):
        _post(db, tid, "talker", "too soon")

    # The same gated peer still receives history through an empty outbox.
    _sent, received, _cursor, _more = _read(db, tid, "talker")
    assert "reply" in [m.content_markdown for m in received]

    # A peer with no cursor reads history regardless of the limit.
    _sent, observed, _cursor, _more = _read(db, tid, "observer")
    assert [m.content_markdown for m in observed] == ["opening", "reply"]


def test_single_peer_topic_is_unaffected_by_the_limit(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = _make_topic(db, rate_limit=1.0, peers=0)
    tid = topic.topic_id

    # No other peers exist, so there is nobody to yield to.
    for i in range(3):
        _post(db, tid, "solo", f"m{i}")


def test_topic_set_rate_limit_validates_and_round_trips(tmp_path):
    db = AgentBusDB(path=str(tmp_path / "bus.sqlite"))
    topic = db.topic_create(name="rl", mode="new", metadata={})
    tid = topic.topic_id

    for bad in (-0.1, 1.1, float("nan")):
        with pytest.raises(ValueError):
            db.topic_set_rate_limit(topic_id=tid, rate_limit=bad, metadata={})

    updated = db.topic_set_rate_limit(topic_id=tid, rate_limit=0.5, metadata={"rate_limit": 0.5})
    assert updated.metadata is not None
    assert updated.metadata["rate_limit"] == 0.5

    cleared = db.topic_set_rate_limit(topic_id=tid, rate_limit=0.0, metadata=None)
    assert cleared.metadata in (None, {}) or "rate_limit" not in (cleared.metadata or {})


def test_rate_limit_gate_is_visible_to_http_error_mapping():
    """The web layer maps RateLimitedError to 429; keep the code name stable."""
    assert str(ErrorCode.RATE_LIMITED) == "RATE_LIMITED"
