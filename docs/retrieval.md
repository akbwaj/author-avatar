# Keyword retrieval

[Project overview](../README.md) · [Local chat setup](conversations.md)

Run shell commands from the repository root.

The existing source, section, and passage tables remain authoritative. Retrieval
adds `passage_fts`, a derived FTS5 virtual table containing the existing passage
ID (unindexed), the section/chapter title, and passage text. FTS5 must be enabled
in the SQLite library used by Python; there is no additional search dependency.
Pydantic is explicitly declared because the HTTP adapter uses its validation API.

### Index and search commands

Run from the repository root using the existing environment:

```sh
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m app.retrieval index --db data/author_avatar.sqlite3
python -m app.retrieval search \
  --db data/author_avatar.sqlite3 \
  --question 'What does the book say about Kriya Yoga?' \
  --top-k 5
```

The index command can be repeated. It replaces all index rows in a transaction
without changing canonical records or their identifiers. It also installs
retrieval-owned triggers that synchronize the index after passage inserts,
updates, and deletes, and after section-title updates. Existing ingestion reruns
therefore remain searchable without a second manual rebuild. A rebuild remains
available to repair the derived index; do not edit its rows directly.

Both CLI commands accept `--db`. Otherwise they use the exported `DATABASE_PATH`
or default to `data/author_avatar.sqlite3`. A nonexistent database is rejected
instead of silently created. Search connections are read-only. The CLI prints
JSON results, prints errors to stderr, and exits with status 2 on invalid input
or unavailable databases/indexes.

### HTTP search

```sh
export DATABASE_PATH=data/author_avatar.sqlite3
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

From another terminal:

```sh
curl -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"question":"What does the book say about Kriya Yoga?","top_k":5}'
```

Response shape: `{"results": [...]}`. Each result contains `passage_id`,
`chapter_title`, `passage_text`, `score`, and the canonical source, edition,
section, chapter-number, anchor, passage-kind, ordinal, and block-range metadata.
Source URL, edition status, and copyright statement are also returned. For
preface/acknowledgments, `chapter_number` is null and `chapter_title` is the
section title; these records are not falsely labelled as numbered chapters.
No page numbers or confidence values are generated.

Questions must be strings of 1–2000 characters, including whitespace, and must
contain at least one letter or number. `top_k` defaults to 5 and must be an
integer from 1 to 10; booleans, fractions, and numeric strings are rejected.
Invalid requests return HTTP 422. A missing database/index returns HTTP 503 with
indexing instructions. A valid search without matches returns HTTP 200 and an
empty results list. Requests never rebuild an index or call a model.

### How ranking works

1. Normalize Unicode to NFC and case-fold the question. Extract letters/numbers,
   dropping punctuation and splitting hyphenated words. Deduplicate terms.
2. Quote every term as an FTS literal and join terms with `OR`. For example,
   `"Kriya Yoga"?` becomes `"kriya" OR "yoga"`. User quotes do not request an
   exact phrase, and words such as AND/NOT are ordinary keywords. Raw user text
   never becomes FTS syntax; the constructed query and result limit are bound
   SQL parameters.
3. FTS5's `unicode61` tokenizer indexes and matches words in passage text and
   titles, case-insensitively, with Latin diacritics removed. It does not perform
   stemming or synonym matching. Any matching query term makes a passage eligible.
4. Rank matches using `bm25(passage_fts, 0.0, 2.0, 1.0)`. Title occurrences have
   weight 2 and passage-text occurrences weight 1. BM25 accounts for term rarity,
   occurrence frequency, and document length. SQLite returns negative BM25;
   **lower/more negative scores rank first**. Passage ID ascending breaks ties
   deterministically. Scores depend on the query and indexed corpus; they are
   not confidence, probability, or a guarantee that a passage answers the question.
5. Join matches back to the canonical passage, section, and source records and
   return up to the requested limit, five by default.

This deliberately simple baseline does not remove stop words, enforce phrase
adjacency, expand queries, or fetch neighboring passages. Common words can
produce weak matches. A title-only match does not establish that the passage
answers a question. All kinds (body, footnote, caption, verse, and front matter)
are searchable, and their metadata must be respected during later generation.
Multiple ingested source versions are searched together; identical prose from
different source IDs represents different provenance, not duplicate index rows.

### Existing ingestion audit and tests

The current database has 48 numbered chapters plus separate preface and author
acknowledgments. Passage IDs remain stable for identical source bytes, edition ID,
and chunk settings. Renaming the source preserves IDs; changed source bytes or
chunking can change them. Citation metadata includes title, chapter number,
section and passage IDs, source and edition IDs, source URL, and available HTML
anchors. The exact print edition is unverified, and no page numbers exist.

No structural blocker to keyword retrieval was found. The inspection report's
contents/title differences, stray `>` in chapter 9, short acknowledgments, and
possible truncated chapter 48 caption still merit manual checking. Hyphenated
line breaks and split sentences can affect matching/context. The chapter 37
caption warning ending in D.C. is a conservative false positive. Retrieval does
not repair or silently reinterpret these source limitations.

```sh
python -m pytest tests/test_retrieval.py
python -m pytest
```

Retrieval tests use a small fictional corpus and verify distinctive keywords,
title-only matching, metadata, ordering/limits, repeat indexing, synchronization
with ingestion, safe punctuation, missing indexes, CLI behavior, and HTTP
validation. They are functional tests, not a measured relevance evaluation of
the real book.

Official references: [SQLite FTS5 and BM25](https://www.sqlite.org/fts5.html),
[FastAPI body fields](https://fastapi.tiangolo.com/tutorial/body-fields/), and
[Pydantic validators](https://docs.pydantic.dev/latest/concepts/validators/).
