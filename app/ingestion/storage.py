"""SQLite schema and atomic replacement of a single source/edition import."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3

from .parser import Book
from .passages import Passage, section_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT NOT NULL,
    edition_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    max_words INTEGER NOT NULL CHECK(max_words > 0),
    PRIMARY KEY(source_id, edition_id)
);
CREATE TABLE IF NOT EXISTS sections (
    section_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    edition_id TEXT NOT NULL,
    section_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    chapter_number INTEGER,
    title TEXT NOT NULL,
    toc_title TEXT,
    html_anchor TEXT,
    section_order INTEGER NOT NULL,
    UNIQUE(source_id, edition_id, section_key),
    FOREIGN KEY(source_id, edition_id) REFERENCES sources(source_id, edition_id)
);
CREATE TABLE IF NOT EXISTS passages (
    passage_id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES sections(section_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    block_start INTEGER NOT NULL,
    block_end INTEGER NOT NULL,
    anchors_json TEXT NOT NULL,
    UNIQUE(section_id, ordinal)
);
"""


def store_book(db_path: Path, book: Book, passages: tuple[Passage, ...], max_words: int) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        with connection:
            connection.execute("""INSERT INTO sources VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, edition_id) DO UPDATE SET
                metadata_json=excluded.metadata_json, max_words=excluded.max_words""",
                (book.source_id, book.edition_id, json.dumps(book.metadata, ensure_ascii=False), max_words))
            # Replace only this source/edition; other imported sources survive.
            connection.execute("DELETE FROM sections WHERE source_id=? AND edition_id=?",
                               (book.source_id, book.edition_id))
            ids = {}
            for order, section in enumerate(book.sections, 1):
                ids[section.key] = section_id(book, section)
                connection.execute("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ids[section.key], book.source_id, book.edition_id, section.key,
                     section.kind, section.chapter_number, section.title, section.toc_title,
                     section.anchor, order))
            connection.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(p.passage_id, ids[p.section_key], p.ordinal, p.kind, p.text,
                  p.block_start, p.block_end, json.dumps(p.anchors)) for p in passages])
