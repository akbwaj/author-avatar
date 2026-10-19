import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.generation.models import AI_IDENTITY
from app.generation.persona import SYSTEM_INSTRUCTIONS
from app.generation.provider import FakeProvider, ModelSettings, ProviderError
from app.generation.rewrite import REWRITE_INSTRUCTIONS
from app.http.search import get_database_path
from app.ingestion.parser import Block, Book, Section
from app.ingestion.passages import split_passages
from app.ingestion.storage import store_book
from app.main import app
from app.memory import storage
from app.memory.context import bounded_history
from app.orchestration.conversation import TurnProvider, conversation_answer
from app.retrieval.index import rebuild_index
from app.retrieval.search import search_passages


@pytest.fixture
def memory_db(tmp_path):
    db = tmp_path / "memory.sqlite3"
    book = Book("fictional-source", "fictional-edition", {"edition_status": "Fictional"}, (
        Section("chapter-01", "chapter", 1, "The Observatory", None, "chapter1", (
            Block("body", "Mira discovered an amber telescope inside the abandoned observatory.", None),)),), ())
    store_book(db, book, split_passages(book), 250)
    rebuild_index(db)
    storage.migrate(db)
    return db


class MemoryFake(FakeProvider):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.bad = False
        self.ambiguous = False

    async def generate(self, request):
        self.requests.append(request)
        if self.bad:
            raise RuntimeError("secret-token rejected draft text")
        if request.system == REWRITE_INSTRUCTIONS:
            if self.ambiguous:
                return json.dumps({"status": "clarification_needed", "search_question": None,
                                   "clarification": "Do you mean Mira or the telescope?"})
            return json.dumps({"status": "resolved", "search_question": "amber telescope", "clarification": None})
        assert request.system == SYSTEM_INSTRUCTIONS
        data = json.loads(request.evidence_json)
        pid = data["passages"][0]["passage_id"]
        return json.dumps({"status": "answered", "answer": f"The book describes an amber telescope. [[{pid}]]",
                           "citations": [pid]})


@pytest.fixture
def memory_client(memory_db, monkeypatch):
    fake = MemoryFake()
    monkeypatch.setattr("app.orchestration.chat.create_provider", lambda settings: fake)
    monkeypatch.setattr("app.orchestration.conversation.create_provider", lambda settings: fake)
    app.dependency_overrides[get_database_path] = lambda: memory_db
    try:
        with TestClient(app, headers=auth("A")) as client:
            yield client, fake
    finally:
        app.dependency_overrides.pop(get_database_path, None)


def auth(user):
    return {"Authorization": "Bearer " + os.environ["DEMO_TOKEN_" + user]}


def create(client, user="A"):
    response = client.post("/conversations", headers=auth(user))
    assert response.status_code == 201
    return response.json()["conversation_id"]


def send(client, cid, message="amber telescope", request_id=None, user="A", **extra):
    return client.post("/chat", headers=auth(user), json={
        "conversation_id": cid, "message": message, "request_id": request_id or str(uuid4()), **extra})


def messages(client, cid, user="A", **params):
    response = client.get(f"/conversations/{cid}/messages", headers=auth(user), params=params)
    assert response.status_code == 200
    return response.json()["messages"]


def test_migration_is_additive_repeatable_and_preserves_history(memory_db):
    original = search_passages(memory_db, "amber")
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    rid = str(uuid4())
    reservation = storage.reserve(memory_db, "demo-user-a", cid, rid, "amber", time.time() + 60)
    storage.finish(memory_db, "demo-user-a", cid, rid, reservation.token, "amber",
                   {"status": "insufficient_evidence", "answer": "No answer.", "sources": [], "request_id": rid})
    storage.migrate(memory_db)
    storage.migrate(memory_db)
    assert search_passages(memory_db, "amber") == original
    assert storage.list_conversations(memory_db, "demo-user-a")[0]["conversation_id"] == cid
    assert len(storage.history(memory_db, "demo-user-a", cid)) == 2
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT version FROM conversation_migrations ORDER BY version").fetchall() == [(1,), (2,)]


def test_user_lists_and_ownership_on_reads_writes_and_deletes(memory_client):
    client, fake = memory_client
    a, b = create(client), create(client, "B")
    assert send(client, a).status_code == 200
    for user, own, other in (("A", a, b), ("B", b, a)):
        listed = client.get("/conversations", headers=auth(user)).json()["conversations"]
        assert [row["conversation_id"] for row in listed] == [own]
        assert client.get(f"/conversations/{other}/messages", headers=auth(user)).status_code == 404
        assert send(client, other, user=user).status_code == 404
        assert client.delete(f"/conversations/{other}", headers=auth(user)).status_code == 404
    assert len(fake.requests) == 1
    assert len(messages(client, a)) == 2
    assert client.delete(f"/conversations/{a}").status_code == 204
    assert client.get(f"/conversations/{a}/messages").status_code == 404


def test_identity_is_derived_from_token_and_body_identity_is_rejected(memory_client):
    client, _ = memory_client
    a = create(client)
    assert send(client, a, user_id="demo-user-b").status_code == 422
    assert client.post("/conversations", json={"owner_id": "demo-user-b"}).json()["owner_id"] == "demo-user-a"
    for header in ("", "Bearer wrong", "Basic abc"):
        assert client.get("/conversations", headers={"Authorization": header}).status_code == 401


def test_authentication_fails_closed_when_unconfigured_or_tokens_equal(memory_client, monkeypatch, caplog):
    client, _ = memory_client
    secret = os.environ["DEMO_TOKEN_A"]
    monkeypatch.setenv("DEMO_TOKEN_B", secret)
    response = client.get("/conversations")
    assert response.status_code == 503
    assert secret not in response.text + caplog.text


@pytest.mark.parametrize("payload", [
    {"question": "amber"}, {"message": "amber"},
    {"conversation_id": "not-a-uuid", "request_id": str(uuid4()), "message": "amber"},
    {"conversation_id": str(uuid4()), "request_id": "bad", "message": "amber"},
])
def test_new_chat_contract_is_required(memory_client, payload):
    client, fake = memory_client
    assert client.post("/chat", json=payload).status_code == 422
    assert fake.requests == []


def test_final_history_stores_original_question_answer_and_resolved_sources(memory_client, memory_db):
    client, fake = memory_client
    cid, rid = create(client), str(uuid4())
    response = send(client, cid, "What about the amber telescope?", rid)
    assert response.status_code == 200
    body, rows = response.json(), messages(client, cid)
    assert body["conversation_id"] == cid
    assert body["request_id"] == rid
    assert [row["sequence"] for row in rows] == [1, 2]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "What about the amber telescope?"
    assert rows[1]["content"] == body["answer"]
    assert rows[1]["sources"] == body["sources"]
    assert rows[1]["metadata"]["status"] == "answered"
    assert rows[1]["content"].startswith(AI_IDENTITY)
    # A new connection sees the committed history; no process-local memory.
    assert storage.history(memory_db, "demo-user-a", cid) == rows


def test_followup_uses_only_its_conversation_and_original_question_is_preserved(memory_client):
    client, fake = memory_client
    a, b = create(client), create(client)
    assert send(client, a).status_code == 200
    assert send(client, a, "Tell me more about it.").status_code == 200
    assert [request.system for request in fake.requests] == [SYSTEM_INSTRUCTIONS, REWRITE_INSTRUCTIONS, SYSTEM_INSTRUCTIONS]
    rewrite = json.loads(fake.requests[1].evidence_json)
    author = json.loads(fake.requests[2].evidence_json)
    assert rewrite["message"] == author["question"] == "Tell me more about it."
    assert rewrite["history"] == author["history"]
    assert rewrite["history"][0]["content"] == "amber telescope"
    assert rewrite["history"][1]["content"] == messages(client, a)[1]["content"]
    assert author["passages"]  # Fresh retrieval on the resolved search question.
    assert send(client, b, "observatory").status_code == 200
    assert "history" not in json.loads(fake.requests[-1].evidence_json)
    assert messages(client, b)[0]["content"] == "observatory"
    assert len(messages(client, a)) == 4
    assert send(client, b, "Tell me more about it.").status_code == 200
    b_context = json.loads(fake.requests[-2].evidence_json)["history"]
    assert b_context[0]["content"] == "observatory"
    assert len(b_context) == 2
    assert len(messages(client, a)) == 4


def test_ambiguous_followup_asks_clarification_without_author_call(memory_client):
    client, fake = memory_client
    cid = create(client)
    send(client, cid)
    fake.ambiguous = True
    response = send(client, cid, "What about her?")
    assert response.status_code == 200
    assert response.json()["status"] == "clarification_needed"
    assert response.json()["sources"] == []
    assert "Mira or the telescope" in response.json()["answer"]
    assert len(fake.requests) == 2
    assert len(messages(client, cid)) == 4


def test_reference_only_first_message_never_guesses(memory_client):
    client, fake = memory_client
    response = send(client, create(client), "What about him?")
    assert response.json()["status"] == "clarification_needed"
    assert fake.requests == []


def test_first_message_with_unresolved_pronouns_can_request_clarification(memory_client):
    client, fake = memory_client
    fake.ambiguous = True
    response = send(client, create(client), "What did she mean by that statement?")
    assert response.json()["status"] == "clarification_needed"
    assert [r.system for r in fake.requests] == [REWRITE_INSTRUCTIONS]
    assert json.loads(fake.requests[0].evidence_json)["history"] == []


@pytest.mark.parametrize("raw", ["not JSON", '{"status":"resolved","search_question":"","clarification":null}',
                                  '{"status":"resolved","search_question":"amber","clarification":"Which?"}',
                                  '{"status":"clarification_needed","clarification":" "}'])
def test_bad_rewrite_never_falls_back_to_guessed_search_or_history(memory_client, monkeypatch, raw):
    client, fake = memory_client
    cid = create(client)
    send(client, cid)
    calls = []

    async def invalid(request):
        calls.append(request)
        return raw

    monkeypatch.setattr(fake, "generate", invalid)
    response = send(client, cid, "Tell me more about it.")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_model_output"
    assert len(calls) == 1
    assert len(messages(client, cid)) == 2


def test_previous_citations_do_not_become_current_book_evidence(memory_client, memory_db, monkeypatch):
    client, fake = memory_client
    cid = create(client)
    first = send(client, cid).json()
    old_pid = first["sources"][0]["passage_id"]
    # Replace canonical IDs and rebuild FTS through its existing sync trigger.
    with sqlite3.connect(memory_db) as con:
        con.execute("UPDATE passages SET passage_id='passage-new'")
    rebuild_index(memory_db)
    original = fake.generate

    async def outdated(request):
        if request.system == REWRITE_INSTRUCTIONS:
            return await original(request)
        data = json.loads(request.evidence_json)
        assert data["passages"][0]["passage_id"] == "passage-new"
        assert old_pid in data["history"][1]["content"]
        return json.dumps({"status": "answered", "answer": f"Old claim [[{old_pid}]]", "citations": [old_pid]})

    monkeypatch.setattr(fake, "generate", outdated)
    assert send(client, cid, "Tell me more about it.").json()["error"]["code"] == "invalid_model_output"
    assert len(messages(client, cid)) == 2


def test_history_budget_keeps_completed_pairs_and_full_storage_separate(memory_client, monkeypatch):
    client, fake = memory_client
    cid = create(client)
    send(client, cid)
    monkeypatch.setenv("MEMORY_CONTEXT_BYTES", "0")
    send(client, cid, "amber telescope")
    assert json.loads(fake.requests[1].evidence_json)["history"] == []
    assert "history" not in json.loads(fake.requests[2].evidence_json)
    assert len(messages(client, cid)) == 4
    rows = messages(client, cid)
    budget = len(json.dumps([{"role": r["role"], "content": r["content"], "sequence": r["sequence"]}
                             for r in rows[-2:]], ensure_ascii=False).encode())
    selected = bounded_history(rows, budget)
    assert [r["sequence"] for r in selected] == [3, 4]
    assert bounded_history(rows, budget - 1) == []
    assert [r["sequence"] for r in messages(client, cid, limit=2, before_sequence=3)] == [1, 2]


def test_completed_duplicate_replays_exact_response_without_messages_or_calls(memory_client):
    client, fake = memory_client
    cid, rid = create(client), str(uuid4())
    first = send(client, cid, request_id=rid)
    again = send(client, cid, request_id=rid)
    assert first.json() == again.json()
    assert len(fake.requests) == 1
    assert len(messages(client, cid)) == 2
    conflict = send(client, cid, "different message", rid)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "request_conflict"


def test_provider_failure_releases_reservation_and_never_stores_draft(memory_client, memory_db, caplog):
    client, fake = memory_client
    cid, rid = create(client), str(uuid4())
    fake.bad = True
    response = send(client, cid, request_id=rid)
    assert response.status_code == 502
    assert "secret-token" not in response.text + caplog.text
    assert messages(client, cid) == []
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT state,response_json FROM conversation_turns").fetchall() == [("failed", None)]
        assert con.execute("SELECT count(*) FROM model_work").fetchone()[0] == 0
    assert send(client, cid, request_id=rid).json()["error"]["code"] == "request_failed"
    fake.bad = False
    assert send(client, cid).status_code == 200
    assert [row["sequence"] for row in messages(client, cid)] == [1, 2]


def test_invalid_author_output_is_not_saved(memory_client, monkeypatch):
    client, fake = memory_client
    cid = create(client)

    async def invalid(request):
        return json.dumps({"status": "answered", "answer": "rejected draft [[invented]]", "citations": ["invented"]})

    monkeypatch.setattr(fake, "generate", invalid)
    assert send(client, cid).json()["error"]["code"] == "invalid_model_output"
    assert messages(client, cid) == []


def test_finalization_failure_rolls_back_both_messages(memory_client, memory_db):
    client, _ = memory_client
    cid = create(client)
    with sqlite3.connect(memory_db) as con:
        con.execute("""CREATE TRIGGER reject_assistant BEFORE INSERT ON messages
                       WHEN new.role='assistant' BEGIN SELECT RAISE(ABORT,'fixture write failure'); END""")
    response = send(client, cid)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "conversation_storage_unavailable"
    assert "fixture write failure" not in response.text
    assert messages(client, cid) == []
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT state FROM conversation_turns").fetchall() == [("failed",)]


def test_failure_with_broken_cleanup_expires_and_recovers(memory_client, memory_db, monkeypatch):
    client, fake = memory_client
    cid = create(client)

    def broken_cleanup(*args):
        raise storage.ConversationError("conversation_storage_unavailable", "Unavailable.", 503)

    monkeypatch.setattr(storage, "abort", broken_cleanup)
    fake.bad = True
    assert send(client, cid).status_code == 502
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT state FROM conversation_turns").fetchall() == [("active",)]
        con.execute("UPDATE conversation_turns SET expires_at=?,deadline=?", (time.time()-1, time.time()-2))
        con.execute("UPDATE model_work SET expires_at=?", (time.time()-1,))
    fake.bad = False
    assert send(client, cid).status_code == 200
    with sqlite3.connect(memory_db) as con:
        assert sorted(row[0] for row in con.execute("SELECT state FROM conversation_turns")) == ["completed", "expired"]
    assert len(messages(client, cid)) == 2


def test_missing_book_index_remains_a_retrieval_error_and_releases_turn(memory_client, memory_db):
    client, fake = memory_client
    cid = create(client)
    with sqlite3.connect(memory_db) as con:
        con.execute("DROP TABLE passage_fts")
    response = send(client, cid)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "retrieval_unavailable"
    assert messages(client, cid) == []
    assert fake.requests == []


def test_overlap_pending_duplicate_and_global_model_capacity(memory_client, memory_db, monkeypatch):
    client, fake = memory_client
    cid, other, rid = create(client), create(client), str(uuid4())
    entered, release = threading.Event(), threading.Event()
    original = fake.generate

    async def held(request):
        entered.set()
        until = time.monotonic() + 4
        while not release.is_set() and time.monotonic() < until:
            await asyncio.sleep(0.005)
        return await original(request)

    monkeypatch.setattr(fake, "generate", held)
    with ThreadPoolExecutor() as executor:
        active = executor.submit(send, client, cid, "amber telescope", rid)
        try:
            assert entered.wait(2)
            assert send(client, cid).json()["error"]["code"] == "conversation_busy"
            assert send(client, cid, request_id=rid).json()["error"]["code"] == "request_in_progress"
            assert client.delete(f"/conversations/{cid}").status_code == 409
            capacity = send(client, other)
            assert capacity.status_code == 503
            assert capacity.json()["error"]["code"] == "model_busy"
            assert messages(client, cid) == []
            # Reads and other short writes complete while the provider is paused.
            assert create(client)
        finally:
            release.set()
        assert active.result(timeout=5).status_code == 200
    assert send(client, other).status_code == 200


def test_sqlite_reservation_race_and_stale_fencing(memory_db):
    owner = "demo-user-a"
    cid = storage.create_conversation(memory_db, owner)["conversation_id"]

    def reserve_one(_):
        try:
            return storage.reserve(memory_db, owner, cid, str(uuid4()), "amber", time.time() + 60)
        except storage.ConversationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve_one, range(2)))
    assert sum(isinstance(r, storage.Reservation) for r in results) == 1
    assert "conversation_busy" in results
    old = next(r for r in results if isinstance(r, storage.Reservation))
    storage.acquire_model(memory_db, owner, cid, old.token, 1)
    with sqlite3.connect(memory_db) as con:
        old_rid = con.execute("SELECT request_id FROM conversation_turns WHERE reservation_token=?", (old.token,)).fetchone()[0]
        con.execute("UPDATE conversation_turns SET expires_at=?,deadline=?", (time.time() - 1, time.time() - 2))
        con.execute("UPDATE model_work SET expires_at=?", (time.time() - 1,))
    new = storage.reserve(memory_db, owner, cid, str(uuid4()), "amber", time.time() + 60)
    storage.acquire_model(memory_db, owner, cid, new.token, 1)
    with pytest.raises(storage.ConversationError, match="expired"):
        storage.finish(memory_db, owner, cid, old_rid, old.token, "amber", {})
    assert new.token != old.token
    storage.abort(memory_db, owner, cid, old.token)
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT state FROM conversation_turns WHERE reservation_token=?", (new.token,)).fetchone()[0] == "active"
        assert con.execute("SELECT reservation_token FROM model_work").fetchall() == [(new.token,)]


def test_global_capacity_is_database_backed_and_owner_checked(memory_db):
    a = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    b = storage.create_conversation(memory_db, "demo-user-b")["conversation_id"]
    ra = storage.reserve(memory_db, "demo-user-a", a, str(uuid4()), "amber", time.time() + 60)
    rb = storage.reserve(memory_db, "demo-user-b", b, str(uuid4()), "amber", time.time() + 60)
    storage.acquire_model(memory_db, "demo-user-a", a, ra.token, 1)
    with pytest.raises(storage.ConversationError) as caught:
        storage.acquire_model(memory_db, "demo-user-b", b, rb.token, 1)
    assert caught.value.code == "model_busy"
    for operation in (
        lambda: storage.reserve(memory_db, "demo-user-b", a, str(uuid4()), "amber", time.time()+60),
        lambda: storage.abort(memory_db, "demo-user-b", a, ra.token),
        lambda: storage.finish(memory_db, "demo-user-b", a, "x", ra.token, "amber", {}),
    ):
        with pytest.raises(storage.ConversationError) as denied:
            operation()
        assert denied.value.status == 404


def test_cancelled_request_releases_turn_and_model_slot(memory_db):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]

    async def scenario():
        entered = asyncio.Event()

        class Held(FakeProvider):
            async def generate(self, request):
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(conversation_answer("amber", memory_db, "demo-user-a", cid,
                                                     str(uuid4()), provider=Held()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    with sqlite3.connect(memory_db) as con:
        assert con.execute("SELECT state FROM conversation_turns").fetchall() == [("failed",)]
        assert con.execute("SELECT count(*) FROM model_work").fetchone()[0] == 0
    assert storage.history(memory_db, "demo-user-a", cid) == []


def test_shared_deadline_includes_rewrite_and_author(memory_client, monkeypatch):
    client, fake = memory_client
    cid = create(client)
    send(client, cid)
    original = fake.generate
    cancelled = []

    async def slow(request):
        try:
            await asyncio.sleep(0.03)
            return await original(request)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(fake, "generate", slow)
    monkeypatch.setenv("CHAT_DEADLINE_SECONDS", "0.05")
    response = send(client, cid, "Tell me more about it.")
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "provider_timeout"
    assert cancelled
    assert len(messages(client, cid)) == 2
    monkeypatch.setattr(fake, "generate", original)
    monkeypatch.setenv("CHAT_DEADLINE_SECONDS", "60")
    assert send(client, cid, "amber").status_code == 200


def test_generation_counter_stops_third_call(memory_db):
    cid = storage.create_conversation(memory_db, "demo-user-a")["conversation_id"]
    r = storage.reserve(memory_db, "demo-user-a", cid, str(uuid4()), "amber", time.time()+60)
    underlying = FakeProvider()
    counted = TurnProvider(memory_db, "demo-user-a", cid, r.token, 1, ModelSettings(), underlying)

    async def scenario():
        await counted.generate(None)
        await counted.generate(None)
        with pytest.raises(ProviderError, match="budget"):
            await counted.generate(None)

    asyncio.run(scenario())
    assert counted.calls == 2
