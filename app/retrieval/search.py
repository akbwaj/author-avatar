"""Safe literal keyword matching and BM25 ranking over indexed passages."""

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from .index import SearchIndexError, database_uri

MAX_QUESTION_LENGTH = 2000


def keyword_query(question: str) -> str:
    """Convert plain user text into an OR of unique, quoted literal terms.

    Quotes, punctuation, and FTS operators never become executable FTS syntax.
    This is keyword matching, not exact-phrase or semantic search.
    """
    if not isinstance(question, str) or not 1 <= len(question) <= MAX_QUESTION_LENGTH:
        raise ValueError(f"question must contain 1–{MAX_QUESTION_LENGTH} characters")
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", question).casefold())
    if not tokens:
        raise ValueError("question must contain at least one letter or number")
    return " OR ".join('"' + token + '"' for token in dict.fromkeys(tokens))


@dataclass(frozen=True)
class SearchResult:
    passage_id: str
    chapter_title: str
    passage_text: str
    score: float
    section_id: str
    section_key: str
    section_kind: str
    chapter_number: int | None
    source_id: str
    edition_id: str
    source_url: str | None
    edition_status: str | None
    copyright_statement: str | None
    html_anchor: str | None
    passage_kind: str
    ordinal: int
    block_start: int
    block_end: int
    anchors: list[str]


def search_passages(db_path: Path, question: str, top_k: int = 5) -> list[SearchResult]:
    """Return up to top_k results, default five, maximum ten.

    Score is SQLite's negative BM25: lower/more negative ranks first. It is
    query/corpus-dependent relevance, NOT probability or confidence. Titles
    receive weight 2; passage text weight 1. Stable passage IDs break ties.
    """
    query = keyword_query(question)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 10:
        raise ValueError("top_k must be an integer from 1 to 10")
    try:
        with closing(sqlite3.connect(database_uri(db_path, "ro"), uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("""SELECT p.passage_id, s.title AS chapter_title,
                    p.text AS passage_text, bm25(passage_fts, 0.0, 2.0, 1.0) AS score,
                    s.section_id, s.section_key, s.kind AS section_kind, s.chapter_number,
                    s.source_id, s.edition_id, s.html_anchor,
                    p.kind AS passage_kind, p.ordinal, p.block_start, p.block_end,
                    p.anchors_json, src.metadata_json
                FROM passage_fts
                JOIN passages p ON p.passage_id=passage_fts.passage_id
                JOIN sections s ON s.section_id=p.section_id
                JOIN sources src ON src.source_id=s.source_id AND src.edition_id=s.edition_id
                WHERE passage_fts MATCH ?
                ORDER BY score ASC, p.passage_id ASC LIMIT ?""", (query, top_k)).fetchall()
    except sqlite3.Error as error:
        raise SearchIndexError(
            "Search database/index unavailable. Ingest the book and run python -m app.retrieval index."
        ) from error
    results = []
    for row in rows:
        values = dict(row)
        metadata = json.loads(values.pop("metadata_json"))
        values["anchors"] = json.loads(values.pop("anchors_json"))
        values.update({key: metadata.get(key) for key in
                       ("source_url", "edition_status", "copyright_statement")})
        results.append(SearchResult(**values))
    return results
