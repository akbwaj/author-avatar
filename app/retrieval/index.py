"""Repeatable FTS5 indexing of the existing canonical passage records."""

from contextlib import closing
from pathlib import Path
import sqlite3


class SearchIndexError(RuntimeError):
    """Database/index is unavailable; distinct from invalid user input."""


INDEX_SQL = """CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
    passage_id UNINDEXED,
    chapter_title,
    passage_text,
    tokenize='unicode61 remove_diacritics 2'
)"""

# These triggers belong to retrieval, and are installed only when indexing is
# requested. Ingestion continues to write its existing canonical tables.
TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS retrieval_passage_insert AFTER INSERT ON passages
    BEGIN
        INSERT INTO passage_fts(passage_id, chapter_title, passage_text)
        SELECT NEW.passage_id, title, NEW.text FROM sections
        WHERE section_id=NEW.section_id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_passage_delete AFTER DELETE ON passages
    BEGIN
        DELETE FROM passage_fts WHERE passage_id=OLD.passage_id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_passage_update
    AFTER UPDATE OF passage_id, section_id, text ON passages
    BEGIN
        DELETE FROM passage_fts WHERE passage_id=OLD.passage_id;
        INSERT INTO passage_fts(passage_id, chapter_title, passage_text)
        SELECT NEW.passage_id, title, NEW.text FROM sections
        WHERE section_id=NEW.section_id;
    END""",
    """CREATE TRIGGER IF NOT EXISTS retrieval_section_title_update
    AFTER UPDATE OF title ON sections
    BEGIN
        UPDATE passage_fts SET chapter_title=NEW.title
        WHERE passage_id IN (SELECT passage_id FROM passages WHERE section_id=NEW.section_id);
    END""",
)


def database_uri(db_path: Path, mode: str) -> str:
    return db_path.resolve().as_uri() + f"?mode={mode}"


def rebuild_index(db_path: Path) -> int:
    """Atomically replace the derived index; never alter source/passage IDs."""
    try:
        # mode=rw prevents creating a misleading empty DB at a misspelled path.
        with closing(sqlite3.connect(database_uri(db_path, "rw"), uri=True)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(INDEX_SQL)
                for sql in TRIGGERS:
                    connection.execute(sql)
                connection.execute("DELETE FROM passage_fts")
                connection.execute("""INSERT INTO passage_fts(passage_id, chapter_title, passage_text)
                    SELECT p.passage_id, s.title, p.text FROM passages p
                    JOIN sections s ON s.section_id=p.section_id ORDER BY p.passage_id""")
                return connection.execute("SELECT COUNT(*) FROM passage_fts").fetchone()[0]
    except sqlite3.Error as error:
        raise SearchIndexError(
            f"Cannot build search index: {error}. Use an ingested database and SQLite with FTS5 support."
        ) from error
