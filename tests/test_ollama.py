import asyncio
from dataclasses import replace
import json
import sqlite3

from helpers import FirstTurnClient as TestClient
import httpx
import pytest

from app.generation.models import AI_IDENTITY
from app.generation.ollama import OllamaProvider
from app.generation.persona import build_request
from app.generation.provider import (
    FakeProvider, ModelSettings, ProviderConfigurationError, ProviderTimeout, create_provider, output_schema,
)
from app.http.search import get_database_path
from app.ingestion.parser import Block, Book, Section
from app.ingestion.passages import split_passages
from app.ingestion.storage import store_book
from app.main import app
from app.memory.storage import migrate
from app.retrieval.index import rebuild_index
from app.retrieval.search import search_passages


@pytest.fixture
def local_db(tmp_path):
    book = Book("fictional-source", "fictional-edition", {
        "source_url": "https://example.invalid/book", "edition_status": "Fictional",
    }, (Section("chapter-01", "chapter", 1, "The Observatory", None, "chapter1", (
        Block("body", "Mira discovered an amber telescope inside the abandoned observatory.", None),
    )),), ())
    db = tmp_path / "local.sqlite3"
    store_book(db, book, split_passages(book), 250)
    rebuild_index(db)
    migrate(db)
    return db


@pytest.fixture
def local_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "1")


def model_details(**changes):
    data = {"capabilities": ["completion"], "template": "standard chat template",
            "model_info": {"general.architecture": "llama", "llama.context_length": 131072}}
    data.update(changes)
    return data


def envelope(raw, **changes):
    data = {"done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": raw}}
    data.update(changes)
    return data


def answered(pid):
    return json.dumps({"status": "answered", "answer": f"The book describes an amber telescope. [[{pid}]]",
                       "citations": [pid]})


@pytest.fixture
def mock_local(local_db, local_environment, monkeypatch):
    pid = search_passages(local_db, "amber")[0].passage_id
    state = {"details": model_details(), "response": envelope(answered(pid)), "calls": []}

    async def handler(request):
        state["calls"].append((request.url.path, json.loads(request.content)))
        if "exception" in state:
            raise state["exception"]
        if request.url.path == "/api/show":
            return httpx.Response(state.get("show_status", 200), json=state["details"])
        assert request.url.path == "/api/chat"
        if "body" in state:
            return httpx.Response(200, content=state["body"])
        return httpx.Response(state.get("status", 200), json=state["response"])

    def factory(settings):
        provider = create_provider(settings)
        assert isinstance(provider, OllamaProvider)
        provider.transport = httpx.MockTransport(handler)
        return provider

    monkeypatch.setattr("app.orchestration.chat.create_provider", factory)
    app.dependency_overrides[get_database_path] = lambda: local_db
    try:
        with TestClient(app) as client:
            yield client, state, pid
    finally:
        app.dependency_overrides.pop(get_database_path, None)


def post(client):
    return client.post("/chat", json={"message": "amber telescope"})


def test_local_answer_uses_schema_and_canonical_sources(mock_local, local_db):
    client, state, pid = mock_local
    response = post(client)
    assert response.status_code == 200
    body = response.json()
    stored = search_passages(local_db, "amber")[0]
    assert body["status"] == "answered"
    assert body["answer"].startswith(AI_IDENTITY)
    assert body["sources"][0]["passage_id"] == pid
    for field in ("source_id", "edition_id", "section_id", "chapter_title", "chapter_number"):
        assert body["sources"][0][field] == getattr(stored, field)
    assert body["sources"][0]["excerpt"] == stored.passage_text
    assert [path for path, _ in state["calls"]] == ["/api/show", "/api/chat"]
    payload = state["calls"][1][1]
    assert payload["stream"] is False
    assert payload["format"]["additionalProperties"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": 16384, "num_predict": 1536}
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert "UNTRUSTED DATA" in payload["messages"][0]["content"]
    expected_schema = output_schema()
    expected_schema["properties"]["citations"]["items"]["enum"] = [pid]
    assert json.dumps(expected_schema, ensure_ascii=False) in payload["messages"][0]["content"]
    assert payload["format"]["properties"]["citations"]["items"]["enum"] == [pid]
    assert "maxLength" not in json.dumps(payload["format"])
    assert output_schema()["properties"]["answer"]["maxLength"] == 6000
    evidence = json.loads(payload["messages"][1]["content"])
    assert evidence["passages"][0]["text"] == stored.passage_text
    assert evidence["passages"][0]["passage_id"] == pid
    assert evidence["passages"][0]["citation_marker"] == f"[[{pid}]]"


def test_local_abstention_is_preserved(mock_local):
    client, state, _ = mock_local
    state["response"] = envelope(json.dumps({"status": "insufficient_evidence", "answer": "I cannot answer from these passages.",
                                            "citations": [], "reason": "The needed detail is absent."}))
    body = post(client).json()
    assert body["status"] == "insufficient_evidence"
    assert body["sources"] == []
    assert body["reason"] == "The needed detail is absent."


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_qwen_model_family_and_configured_name(mock_local, monkeypatch, architecture):
    client, state, _ = mock_local
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b-instruct")
    state["details"] = model_details(model_info={
        "general.architecture": architecture, f"{architecture}.context_length": 262144,
    })
    assert post(client).status_code == 200
    assert all(payload["model"] == "qwen3:4b-instruct" for _, payload in state["calls"])


@pytest.mark.parametrize("changes,code,status", [
    ({"show_status": 404}, "model_unavailable", 503),
    ({"status": 404}, "model_unavailable", 503),
    ({"status": 503}, "provider_unavailable", 503),
    ({"exception": httpx.ConnectError("secret prompt")}, "provider_unavailable", 503),
    ({"exception": httpx.ReadTimeout("secret prompt")}, "provider_timeout", 504),
    ({"status": 500}, "provider_failure", 502),
    ({"status": 400}, "provider_failure", 502),
    ({"status": 302}, "provider_failure", 502),
    ({"body": "not JSON secret prompt"}, "invalid_model_output", 502),
    ({"body": b"x" * 128001}, "invalid_model_output", 502),
    ({"response": []}, "invalid_model_output", 502),
    ({"response": {"error": "secret prompt"}}, "invalid_model_output", 502),
    ({"response": envelope("{}")}, "invalid_model_output", 502),
    ({"response": envelope("not JSON")}, "invalid_model_output", 502),
    ({"response": envelope(json.dumps({"status": "insufficient_evidence", "answer": "x" * 6001,
                                      "citations": [], "reason": "No evidence."}))}, "invalid_model_output", 502),
    ({"response": envelope("{}", done=False)}, "invalid_model_output", 502),
    ({"response": envelope("{}", done_reason="length")}, "invalid_model_output", 502),
    ({"response": envelope("{}", message={"content": 7})}, "invalid_model_output", 502),
])
def test_local_errors_are_explicit_and_not_retried(mock_local, changes, code, status, caplog):
    client, state, _ = mock_local
    state.update(changes)
    response = post(client)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "answer" not in response.json()
    assert "secret prompt" not in response.text + caplog.text
    assert sum(path == "/api/chat" for path, _ in state["calls"]) <= 1


@pytest.mark.parametrize("mode", ["unknown", "metadata", "quotation", "unsupplied"])
def test_local_grounding_checks_remain_enforced(mock_local, local_db, mode):
    client, state, pid = mock_local
    data = json.loads(answered(pid))
    if mode == "unknown":
        data = json.loads(answered("invented-id"))
    elif mode == "metadata":
        data["chapter_title"] = "Invented"
    elif mode == "quotation":
        data["answer"] = f'The book says "a golden spaceship". [[{pid}]]'
    else:
        with sqlite3.connect(local_db) as connection:
            section = connection.execute("SELECT section_id FROM passages").fetchone()[0]
            connection.execute("UPDATE passages SET text=?", ("amber " * 4000,))
            connection.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               ("passage-small", section, 2, "body", "amber", 1, 1, "[]"))
    state["response"] = envelope(json.dumps(data))
    response = post(client)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_model_output"
    if mode == "unsupplied":
        payload = state["calls"][1][1]
        assert [p["passage_id"] for p in json.loads(payload["messages"][1]["content"])["passages"]] == ["passage-small"]


@pytest.mark.parametrize("details", [
    model_details(remote_model="cloud-alias"), model_details(template="x" * 2049),
    model_details(model_info={"general.architecture": "llama", "llama.context_length": 4096}),
    model_details(model_info={"general.architecture": "unknown", "unknown.context_length": 32768}),
    model_details(model_info={}), model_details(capabilities=[]),
])
def test_incompatible_model_is_rejected_before_generation(mock_local, details):
    client, state, _ = mock_local
    state["details"] = details
    response = post(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_configuration_error"
    assert len(state["calls"]) == 1


@pytest.mark.parametrize("name,value", [
    ("OLLAMA_MODEL", ""), ("OLLAMA_MODEL", "model:cloud"),
    ("OLLAMA_BASE_URL", "https://ollama.com"), ("OLLAMA_BASE_URL", "http://localhost:bad"),
    ("OLLAMA_BASE_URL", "http://user:secret@localhost:11434"),
    ("OLLAMA_BASE_URL", "http://localhost:11434/api"),
    ("OLLAMA_NUM_CTX", "4096"), ("OLLAMA_NUM_CTX", "999999"), ("OLLAMA_NUM_CTX", "bad"),
    ("MODEL_TIMEOUT_SECONDS", "NaN"), ("MODEL_TIMEOUT_SECONDS", "61"),
])
def test_invalid_local_configuration(local_environment, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ProviderConfigurationError):
        ModelSettings.from_environment()


def test_new_provider_setting_takes_precedence_and_fake_remains_available(local_environment, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "unsupported")
    assert isinstance(create_provider(ModelSettings.from_environment()), OllamaProvider)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert isinstance(create_provider(ModelSettings.from_environment()), FakeProvider)


def test_utf8_context_budget_preserves_whole_passages(local_db):
    stored = search_passages(local_db, "amber")[0]
    settings = ModelSettings(provider="ollama", model="llama3.2:3b", context_tokens=8192)
    huge = replace(stored, passage_id="large-id", passage_text="amber " + "界" * 3000)
    request, supplied = build_request("amber", [huge, stored], max_prompt_bytes=settings.max_prompt_bytes)
    assert supplied == [stored]
    assert request.prompt_bytes <= settings.max_prompt_bytes
    assert json.loads(request.evidence_json)["passages"][0]["text"] == stored.passage_text


def test_empty_retrieval_and_oversized_evidence_skip_network(mock_local, local_db):
    client, state, _ = mock_local
    assert client.post("/chat", json={"message": "absentxyz"}).json()["status"] == "insufficient_evidence"
    with sqlite3.connect(local_db) as connection:
        connection.execute("UPDATE passages SET text=?", ("amber " * 4000,))
    assert post(client).json()["status"] == "insufficient_evidence"
    assert state["calls"] == []


def test_total_deadline_cancels_local_http_work(local_db):
    cancelled = []

    async def slow(request):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    settings = ModelSettings(provider="ollama", model="llama3.2:3b", timeout_seconds=0.01)
    provider = OllamaProvider(settings, transport=httpx.MockTransport(slow))
    request, _ = build_request("amber", search_passages(local_db, "amber"))
    with pytest.raises(ProviderTimeout):
        asyncio.run(provider.generate(request))
    assert cancelled == [True]
