from dataclasses import replace
import json
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app.http.search import get_database_path
from app.ingestion.parser import Block, Book, Section
from app.ingestion.passages import split_passages
from app.ingestion.storage import store_book
from app.main import app
from app.retrieval.index import SearchIndexError, rebuild_index
from app.retrieval.search import search_passages


@pytest.fixture
def corpus(tmp_path):
    # Entirely fictional evidence. No book, model, network, or production DB.
    book = Book("fictional-source", "fictional-edition", {
        "source_url": "https://example.invalid/fictional-book",
        "edition_status": "Fictional test edition",
        "copyright_statement": "Fictional fixture",
    }, (
        Section("chapter-01", "chapter", 1, "The Observatory", "The Observatory", "chap1", (
            Block("body", "Mira discovered an amber telescope inside the abandoned observatory.", None),)),
        Section("chapter-02", "chapter", 2, "The Orchard", "The Orchard", "chap2", (
            Block("body", "The café served apples beside the orchard.", None),)),
        Section("chapter-03", "chapter", 3, "Navigating Rivers", "Navigating Rivers", "chap3", tuple(
            Block("body", f"River travelers used a compass near marker {i}.", None) for i in range(12))),
        Section("preface", "preface", None, "Editor's Preface", None, "preface", (
            Block("body", "An editor introduces this fictional expedition.", None),)),
    ), ())
    passages = split_passages(book, 12)
    db = tmp_path / "fictional.sqlite3"
    store_book(db, book, passages, 12)
    return db, book, passages


@pytest.fixture
def indexed(corpus):
    rebuild_index(corpus[0])
    return corpus


@pytest.fixture
def client(indexed):
    app.dependency_overrides[get_database_path] = lambda: indexed[0]
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_database_path, None)


def test_distinctive_keywords_and_citation_metadata(indexed):
    db, book, passages = indexed
    results = search_passages(db, "amber telescope")
    assert len(results) == 1
    result = results[0]
    assert result.passage_id == passages[0].passage_id
    assert result.passage_text == passages[0].text
    assert result.chapter_number == 1
    assert result.chapter_title == "The Observatory"
    assert result.section_key == "chapter-01"
    assert result.html_anchor == "chap1"
    assert result.source_id == book.source_id
    assert result.edition_id == book.edition_id
    assert result.source_url == book.metadata["source_url"]
    assert result.edition_status == "Fictional test edition"
    assert result.score < 0


def test_title_only_search_and_front_matter_attribution(indexed):
    db, _, passages = indexed
    assert "Orchard" not in passages[0].text
    result = search_passages(db, "Orchard")[0]
    assert result.chapter_number == 2
    assert result.chapter_title == "The Orchard"
    preface = search_passages(db, "expedition")[0]
    assert preface.section_kind == "preface"
    assert preface.chapter_number is None


def test_repeat_rebuild_does_not_duplicate_or_change_canonical_data(indexed):
    db, _, passages = indexed
    before = search_passages(db, "river compass", 10)
    assert rebuild_index(db) == len(passages)
    assert rebuild_index(db) == len(passages)
    assert search_passages(db, "river compass", 10) == before
    with sqlite3.connect(db) as connection:
        count, unique_count = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT passage_id) FROM passage_fts").fetchone()
        originals = connection.execute("SELECT passage_id, text FROM passages ORDER BY passage_id").fetchall()
    assert count == unique_count == len(passages)
    assert originals == sorted((p.passage_id, p.text) for p in passages)


def test_default_limit_requested_limit_order_and_no_matches(indexed):
    db = indexed[0]
    assert len(search_passages(db, "river")) == 5
    results = search_passages(db, "river", 10)
    assert len(results) == 10
    assert [(r.score, r.passage_id) for r in results] == sorted(
        (r.score, r.passage_id) for r in results)
    ranked = search_passages(db, "amber river", 10)
    assert ranked[0].chapter_number == 1  # Rare amber ranks ahead of common river.
    assert search_passages(db, "absentwordxyz") == []


@pytest.mark.parametrize("question", [
    '"amber telescope"?!', 'amber: (telescope) + * -',
    "amber'; DROP TABLE passages; --", 'amber OR NOT NEAR("telescope")',
    '“AMBER”—telescope…', 'café?', 'cafe\u0301!',
])
def test_punctuation_quotes_operators_and_unicode_are_safe(indexed, question):
    db = indexed[0]
    assert search_passages(db, question)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM passages").fetchone()[0] > 0


@pytest.mark.parametrize("question", ["", "   \n", '"?!+-_*()"', "🧘", "a" * 2001])
def test_empty_or_invalid_questions_rejected(indexed, question):
    with pytest.raises(ValueError):
        search_passages(indexed[0], question)


@pytest.mark.parametrize("top_k", [0, 11, -1, True, 1.5, "5"])
def test_invalid_limits_rejected(indexed, top_k):
    with pytest.raises(ValueError):
        search_passages(indexed[0], "amber", top_k)


def test_long_valid_question_does_not_break_fts_parser(indexed):
    question = " ".join(str(i) for i in range(500))
    assert len(question) <= 2000
    assert isinstance(search_passages(indexed[0], question), list)


def test_repeat_and_changed_ingestion_keep_index_in_sync(indexed):
    db, book, passages = indexed
    before = search_passages(db, "amber")
    store_book(db, book, passages, 12)
    assert search_passages(db, "amber") == before
    changed_section = replace(book.sections[0], blocks=(
        Block("body", "Mira now carries a violet sextant.", None),))
    changed_book = replace(book, sections=(changed_section, *book.sections[1:]))
    changed_passages = split_passages(changed_book, 12)
    store_book(db, changed_book, changed_passages, 12)
    assert search_passages(db, "amber telescope") == []
    assert search_passages(db, "violet sextant")[0].passage_id == changed_passages[0].passage_id
    with sqlite3.connect(db) as connection:
        count, unique_count = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT passage_id) FROM passage_fts").fetchone()
    assert count == unique_count == len(changed_passages)


def test_title_and_passage_edits_and_deletes_update_index(indexed):
    db, _, passages = indexed
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE sections SET title=? WHERE section_key=?",
                           ("Celestial Instruments", "chapter-01"))
        connection.execute("UPDATE passages SET text=? WHERE passage_id=?",
                           ("A silver astrolabe.", passages[0].passage_id))
    assert search_passages(db, "observatory amber telescope") == []
    result = search_passages(db, "celestial astrolabe")[0]
    assert result.chapter_title == "Celestial Instruments"
    assert result.passage_text == "A silver astrolabe."
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM passages WHERE passage_id=?", (result.passage_id,))
    assert search_passages(db, "astrolabe") == []


def test_missing_index_and_database_have_actionable_error(corpus, tmp_path):
    with pytest.raises(SearchIndexError, match="index unavailable"):
        search_passages(corpus[0], "amber")
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(SearchIndexError):
        rebuild_index(missing)
    assert not missing.exists()


def test_http_search_matches_service(client, indexed):
    response = client.post("/search", json={"question": '"amber telescope"?', "top_k": 3})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["passage_id"] == indexed[2][0].passage_id
    assert result["chapter_title"] == "The Observatory"
    assert result["chapter_number"] == 1
    assert result["source_id"] == "fictional-source"
    assert result["score"] < 0
    assert client.post("/search", json={"question": "river"}).json()["results"] == [
        json.loads(json.dumps(r.__dict__)) for r in search_passages(indexed[0], "river")]
    assert client.post("/search", json={"question": "absentwordxyz"}).json() == {"results": []}


@pytest.mark.parametrize("top_k", [1, 10])
def test_http_limit_boundaries(client, top_k):
    response = client.post("/search", json={"question": "river", "top_k": top_k})
    assert response.status_code == 200
    assert len(response.json()["results"]) == top_k


def test_http_question_at_length_limit_is_accepted(client):
    response = client.post("/search", json={"question": "a" * 2000})
    assert response.status_code == 200


@pytest.mark.parametrize("payload", [
    {"question": ""}, {"question": "  "}, {"question": "?!"},
    {"question": "a" * 2001}, {"question": 123}, {},
    {"question": "amber", "top_k": 0}, {"question": "amber", "top_k": 11},
    {"question": "amber", "top_k": True}, {"question": "amber", "top_k": "5"},
    {"question": "amber", "top_k": 2.5},
])
def test_http_validation(client, payload):
    assert client.post("/search", json=payload).status_code == 422


def test_http_missing_index_returns_503(corpus):
    app.dependency_overrides[get_database_path] = lambda: corpus[0]
    try:
        with TestClient(app) as client:
            response = client.post("/search", json={"question": "amber"})
            assert response.status_code == 503
            assert "python -m app.retrieval index" in response.json()["detail"]
            assert client.get("/health").status_code == 200
    finally:
        app.dependency_overrides.pop(get_database_path, None)


def test_cli_indexes_and_searches_fictional_database(corpus):
    db = corpus[0]
    base = [sys.executable, "-m", "app.retrieval"]
    index = subprocess.run([*base, "index", "--db", str(db)], capture_output=True, text=True)
    assert index.returncode == 0, index.stderr
    result = subprocess.run([*base, "search", "--db", str(db), "--question", '"amber telescope"?',
                             "--top-k", "1"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["results"][0]["chapter_number"] == 1
    empty = subprocess.run([*base, "search", "--db", str(db), "--question", ""],
                           capture_output=True, text=True)
    assert empty.returncode == 2
