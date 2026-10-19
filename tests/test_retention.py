"""UTC retention boundaries and actual HTTP/model flows, with no live model."""
import asyncio
import importlib
import json
import sqlite3
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.generation.rewrite import REWRITE_INSTRUCTIONS
from app.main import app
from app.memory import retention, storage
from app.memory.context import bounded_history
# Reuse the existing fictional-book database, fake model and HTTP fixtures.
from test_memory import memory_db, memory_client, auth, create, send, messages


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 2_000_000_000.0

        def advance(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(retention, "utc_now", lambda: clock.now)
    return clock


def complete(db, cid, clock, created_at, *, owner="demo-user-a", text="stored secret"):
    current = clock.now
    clock.now = created_at
    rid = str(uuid4())
    try:
        reserved = storage.reserve(db, owner, cid, rid, text, clock.now + 60)
        storage.finish(db, owner, cid, rid, reserved.token, text, {
            "status": "answered", "answer": text + " answer", "sources": [{"excerpt": text + " citation"}],
            "reason": text + " metadata", "request_id": rid, "conversation_id": cid,
        })
    finally:
        clock.now = current
    return rid


def test_strict_120_hour_boundary_mixed_age_and_idempotency(memory_db, clock, caplog):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    boundary = clock.now - 120 * 60 * 60
    old = complete(memory_db, cid, clock, boundary - .001, text="EXPIRED_PRIVATE_CONTENT")
    exact = complete(memory_db, cid, clock, boundary)
    newer = complete(memory_db, cid, clock, boundary + .001)
    assert retention.RETENTION_SECONDS == 432000
    assert retention.cleanup(memory_db) == {"messages_deleted": 2, "response_caches_deleted": 1}
    assert retention.cleanup(memory_db) == {"messages_deleted": 0, "response_caches_deleted": 0}
    rows = storage.history(memory_db, "demo-user-a", cid)
    assert [row["request_id"] for row in rows] == [exact, exact, newer, newer]
    assert [row["sequence"] for row in rows] == [3, 4, 5, 6]
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1
        assert con.execute("SELECT state,message_hash,response_json FROM conversation_turns WHERE request_id=?", (old,)).fetchone() == ("completed", "", None)
        stored = str(con.execute("SELECT * FROM messages").fetchall()) + str(con.execute("SELECT * FROM conversation_turns").fetchall())
    assert "EXPIRED_PRIVATE_CONTENT" not in stored + caplog.text
    # Boundary messages and their replay cache are still valid, without a new turn.
    assert storage.reserve(memory_db, "demo-user-a", cid, exact, "stored secret", clock.now + 60).response is not None


def test_individual_deletion_keeps_newer_half_and_does_not_refresh_age(memory_db, clock):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    rid = complete(memory_db, cid, clock, retention.cutoff() - 1)
    with sqlite3.connect(memory_db) as con:
        con.execute("UPDATE messages SET created_at=? WHERE request_id=? AND role='assistant'", (retention.cutoff() + 1, rid))
    before = storage.history(memory_db, "demo-user-a", cid)
    assert len(before) == 1 and before[0]["role"] == "assistant"
    assert bounded_history(before, 4000) == []
    retention.cleanup(memory_db)
    assert storage.history(memory_db, "demo-user-a", cid) == before
    clock.advance(1.001)
    assert storage.history(memory_db, "demo-user-a", cid) == []
    assert retention.cleanup(memory_db)["messages_deleted"] == 1


def test_migration_from_version_one_preserves_data_and_cleanup_repairs_orphan_cache(memory_db, clock):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    rid = complete(memory_db, cid, clock, clock.now)
    with sqlite3.connect(memory_db) as con:
        con.execute("DELETE FROM conversation_migrations WHERE version=2")
        con.execute("DROP INDEX messages_created_at")
        con.execute("DROP INDEX retained_turn_caches")
    storage.migrate(memory_db)
    assert len(storage.history(memory_db, "demo-user-a", cid)) == 2
    with sqlite3.connect(memory_db) as con:
        con.execute("DELETE FROM messages")
    assert retention.cleanup(memory_db) == {"messages_deleted": 0, "response_caches_deleted": 1}
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT message_hash,response_json FROM conversation_turns WHERE request_id=?", (rid,)).fetchone() == ("", None)
    with pytest.raises(storage.ConversationError) as error:
        storage.reserve(memory_db, "demo-user-a", cid, rid, "stored secret", clock.now + 60)
    assert error.value.code == "request_expired"


@pytest.mark.parametrize("cleanup_first", [False, True])
@pytest.mark.parametrize("expired_role", ["both", "user", "assistant"])
def test_expired_duplicate_cannot_replay_or_execute_again(memory_client, memory_db, clock, cleanup_first, expired_role):
    client, fake = memory_client
    cid = create(client)
    rid = complete(memory_db, cid, clock, retention.cutoff() - 1)
    if expired_role != "both":
        with sqlite3.connect(memory_db) as con:
            con.execute("UPDATE messages SET created_at=? WHERE request_id=? AND role<>?", (clock.now, rid, expired_role))
    if cleanup_first:
        retention.cleanup(memory_db)
    for message in ("stored secret", "different message"):
        response = send(client, cid, message, rid)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "request_expired"
        assert "stored secret" not in response.text
    assert fake.requests == []
    assert len(messages(client, cid)) == (0 if expired_role == "both" else 1)
    assert send(client, cid).status_code == 200
    assert [row["sequence"] for row in messages(client, cid)][-2:] == [3, 4]


def test_expired_history_hidden_before_cleanup_and_no_prior_context(memory_client, memory_db, clock):
    client, fake = memory_client
    cid = create(client)
    complete(memory_db, cid, clock, retention.cutoff() - 1, text="EXPIRED_PRIVATE_CONTENT")
    assert messages(client, cid) == []
    # Reading or listing a conversation does not renew timestamps.
    assert client.get("/conversations").json()["conversations"][0]["conversation_id"] == cid
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT max(created_at) FROM messages").fetchone()[0] < retention.cutoff()
    response = send(client, cid, "Tell me more about it.")
    assert response.status_code == 200
    assert response.json()["status"] == "clarification_needed"
    assert fake.requests == []
    assert all("EXPIRED_PRIVATE_CONTENT" not in row["content"] for row in messages(client, cid))


def test_only_unexpired_completed_turns_reach_models(memory_client, memory_db, clock):
    client, fake = memory_client
    cid = create(client)
    complete(memory_db, cid, clock, retention.cutoff() - 1, text="EXPIRED_PRIVATE_CONTENT")
    complete(memory_db, cid, clock, clock.now - 1, text="RECENT_PRIVATE_CONTEXT")
    assert send(client, cid, "Tell me more about it.").status_code == 200
    assert len(fake.requests) == 2
    for request in fake.requests:
        data = json.loads(request.evidence_json)
        assert "EXPIRED_PRIVATE_CONTENT" not in request.evidence_json
        assert "RECENT_PRIVATE_CONTEXT" in json.dumps(data["history"])
    # Logical expiry does not require deleting rows first.
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT count(*) FROM messages WHERE created_at<?", (retention.cutoff(),)).fetchone()[0] == 2


def test_orphans_cannot_pair_with_another_request(memory_db, clock):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    a = complete(memory_db, cid, clock, clock.now - 1)
    b = complete(memory_db, cid, clock, clock.now - 1)
    c = complete(memory_db, cid, clock, clock.now - 1)
    with sqlite3.connect(memory_db) as con:
        con.execute("UPDATE messages SET created_at=? WHERE (request_id=? AND role='assistant') OR (request_id=? AND role='user')",
                    (retention.cutoff() - 1, a, b))
    rows = storage.history(memory_db, "demo-user-a", cid)
    assert [row["request_id"] for row in rows] == [a, b, c, c]
    assert [row["sequence"] for row in bounded_history(rows, 4000)] == [5, 6]


def test_cleanup_preserves_active_reservations_leases_and_monotonic_sequences(memory_db, clock):
    owner = "demo-user-a"
    cid = storage.create_conversation(memory_db, owner)["conversation_id"]
    old = complete(memory_db, cid, clock, retention.cutoff() - 1)
    rid = str(uuid4())
    reserved = storage.reserve(memory_db, owner, cid, rid, "new message", clock.now + 60)
    storage.acquire_model(memory_db, owner, cid, reserved.token, 1)
    with sqlite3.connect(memory_db) as con:
        active = con.execute("SELECT * FROM conversation_turns WHERE request_id=?", (rid,)).fetchone()
        lease = con.execute("SELECT * FROM model_work").fetchall()
    retention.cleanup(memory_db)
    retention.cleanup(memory_db)
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT * FROM conversation_turns WHERE request_id=?", (rid,)).fetchone() == active
        assert con.execute("SELECT * FROM model_work").fetchall() == lease
    storage.finish(memory_db, owner, cid, rid, reserved.token, "new message", {"status": "answered", "answer": "fresh answer", "sources": []})
    assert [row["sequence"] for row in storage.history(memory_db, owner, cid)] == [3, 4]
    with pytest.raises(storage.ConversationError, match="expired"):
        storage.reserve(memory_db, owner, cid, old, "stored secret", clock.now + 60)


def test_cleanup_across_multiple_batches(memory_db, clock, monkeypatch):
    monkeypatch.setattr(retention, "CLEANUP_BATCH_SIZE", 3)
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    for _ in range(4):
        complete(memory_db, cid, clock, retention.cutoff() - 1)
    fresh = complete(memory_db, cid, clock, clock.now)
    assert retention.cleanup(memory_db) == {"messages_deleted": 8, "response_caches_deleted": 4}
    assert retention.cleanup(memory_db) == {"messages_deleted": 0, "response_caches_deleted": 0}
    assert [row["request_id"] for row in storage.history(memory_db, "demo-user-a", cid)] == [fresh, fresh]


@pytest.mark.parametrize("stage", ["rewrite", "author"])
@pytest.mark.parametrize("run_cleanup", [False, True])
def test_inflight_expiry_discards_draft_without_restoring_history(memory_client, memory_db, clock, monkeypatch, stage, run_cleanup):
    client, fake = memory_client
    cid = create(client)
    complete(memory_db, cid, clock, retention.cutoff() + .5)
    original = fake.generate

    async def expire_during_model(request):
        if (request.system == REWRITE_INSTRUCTIONS) == (stage == "rewrite"):
            clock.advance(1)
            if run_cleanup:
                # This runs during the awaited model call, using another DB connection.
                await asyncio.to_thread(retention.cleanup, memory_db)
        return await original(request)

    monkeypatch.setattr(fake, "generate", expire_during_model)
    rid = str(uuid4())
    response = send(client, cid, "Tell me more about it.", rid)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "history_expired"
    assert messages(client, cid) == []
    assert len(fake.requests) == (1 if stage == "rewrite" else 2)
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT state,response_json FROM conversation_turns WHERE request_id=?", (rid,)).fetchone() == ("failed", None)
        assert con.execute("SELECT count(*) FROM model_work").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM messages WHERE request_id=?", (rid,)).fetchone()[0] == 0
    monkeypatch.setattr(fake, "generate", original)
    assert send(client, cid).status_code == 200


def test_cleanup_keeps_owner_isolation_and_manual_delete(memory_client, memory_db, clock):
    client, _ = memory_client
    a, b = create(client), create(client, "B")
    complete(memory_db, a, clock, retention.cutoff() - 1)
    complete(memory_db, b, clock, clock.now, owner="demo-user-b")
    retention.cleanup(memory_db)
    for user, own, other in (("A", a, b), ("B", b, a)):
        assert client.get(f"/conversations/{other}/messages", headers=auth(user)).status_code == 404
        assert send(client, other, user=user).status_code == 404
        assert client.delete(f"/conversations/{other}", headers=auth(user)).status_code == 404
        assert [c["conversation_id"] for c in client.get("/conversations", headers=auth(user)).json()["conversations"]] == [own]
    assert messages(client, a) == []
    assert len(messages(client, b, user="B")) == 2
    assert client.delete(f"/conversations/{a}").status_code == 204


def test_history_expiring_during_lease_acquisition_never_reaches_model(memory_client, memory_db, clock, monkeypatch):
    client, fake = memory_client
    cid = create(client)
    complete(memory_db, cid, clock, retention.cutoff() + .5)
    acquire = storage.acquire_model

    def delayed_acquire(*args):
        acquire(*args)
        clock.advance(1)

    monkeypatch.setattr(storage, "acquire_model", delayed_acquire)
    response = send(client, cid, "Tell me more about it.")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "history_expired"
    assert fake.requests == []
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT count(*) FROM model_work").fetchone()[0] == 0


def test_startup_deletes_expired_content_and_preserves_conversation(memory_db, clock, monkeypatch):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    complete(memory_db, cid, clock, retention.cutoff() - 1)
    monkeypatch.setenv("DATABASE_PATH", str(memory_db))
    with TestClient(app, headers=auth("A")) as client:
        assert client.get("/health").status_code == 200
        assert messages(client, cid) == []
        assert client.get("/conversations").json()["conversations"][0]["conversation_id"] == cid
        with sqlite3.connect(memory_db) as con:
            assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
            assert con.execute("SELECT response_json FROM conversation_turns").fetchone()[0] is None


def test_hourly_schedule_with_controlled_clock(memory_db, clock, monkeypatch):
    main = importlib.import_module("app.main")
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    complete(memory_db, cid, clock, retention.cutoff() + 3599)
    monkeypatch.setenv("DATABASE_PATH", str(memory_db))
    sleeps = []

    async def simulated_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError
        clock.advance(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", simulated_sleep)
    assert len(storage.history(memory_db, "demo-user-a", cid)) == 2
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.hourly_retention_cleanup())
    assert sleeps == [3600, 3600]
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_cleanup_command_is_repeatable(memory_db, clock):
    # Subprocess has the real server clock; an epoch timestamp is certainly old.
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    complete(memory_db, cid, clock, 1)
    command = [sys.executable, "-m", "app.memory", "--db", str(memory_db), "--cleanup"]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "2 messages and 1 response caches deleted" in first.stdout
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert "0 messages and 0 response caches deleted" in second.stdout
    assert "stored secret" not in first.stdout + second.stdout
