"""Additive migration and short, fenced SQLite turn transactions."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from . import retention


class ConversationError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS conversations(
    conversation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence > 0)
);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner_id, updated_at);
CREATE TABLE IF NOT EXISTS conversation_turns(
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    reservation_token TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK(state IN ('active','completed','failed','expired')),
    deadline REAL NOT NULL,
    expires_at REAL NOT NULL,
    response_json TEXT,
    PRIMARY KEY(conversation_id, request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_conversation_turn
    ON conversation_turns(conversation_id) WHERE state='active';
CREATE TABLE IF NOT EXISTS messages(
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    request_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(conversation_id, sequence),
    UNIQUE(conversation_id, request_id, role)
);
CREATE TABLE IF NOT EXISTS model_work(
    reservation_token TEXT PRIMARY KEY REFERENCES conversation_turns(reservation_token) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS retained_turn_caches ON conversation_turns(conversation_id, request_id)
    WHERE state='completed' AND (response_json IS NOT NULL OR message_hash<>'');
INSERT OR IGNORE INTO conversation_migrations VALUES(1);
INSERT OR IGNORE INTO conversation_migrations VALUES(2);
"""


@contextmanager
def connection(db_path: Path, *, write: bool = False):
    # Never create a misspelled/new book DB during a request. The migration CLI
    # and endpoints require an existing SQLite file, as retrieval does.
    try:
        con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.25)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        # Clear overwritten/deleted content from ordinary SQLite database pages.
        # Backups and forensic disk erasure require separate operational policies.
        con.execute("PRAGMA secure_delete=ON")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()
    except sqlite3.Error:
        raise ConversationError("conversation_storage_unavailable", "Conversation storage unavailable. Run the memory migration.", 503) from None


def migrate(db_path: Path) -> None:
    # Add tables atomically, without changing user_version, book tables or FTS.
    with connection(db_path) as con:
        try:
            con.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        except BaseException:
            con.rollback()
            raise


def _owned(con, owner_id: str, conversation_id: str):
    row = con.execute("SELECT * FROM conversations WHERE conversation_id=? AND owner_id=?",
                      (conversation_id, owner_id)).fetchone()
    if row is None:
        raise ConversationError("conversation_not_found", "Conversation not found.", 404)
    return row


def create_conversation(db_path: Path, owner_id: str) -> dict:
    now, cid = retention.utc_now(), str(uuid4())
    with connection(db_path, write=True) as con:
        con.execute("INSERT INTO conversations VALUES(?,?,?,?,1)", (cid, owner_id, now, now))
        return dict(_owned(con, owner_id, cid))


def list_conversations(db_path: Path, owner_id: str) -> list[dict]:
    with connection(db_path) as con:
        return [dict(row) for row in con.execute(
            "SELECT * FROM conversations WHERE owner_id=? ORDER BY updated_at DESC, conversation_id", (owner_id,))]


def _recover(con, conversation_id: str, now: float):
    con.execute("UPDATE conversation_turns SET state='expired' WHERE conversation_id=? AND state='active' AND expires_at<=?",
                (conversation_id, now))
    con.execute("DELETE FROM model_work WHERE expires_at<=?", (now,))


def delete_conversation(db_path: Path, owner_id: str, conversation_id: str):
    with connection(db_path, write=True) as con:
        _owned(con, owner_id, conversation_id)
        _recover(con, conversation_id, retention.utc_now())
        if con.execute("SELECT 1 FROM conversation_turns WHERE conversation_id=? AND state='active'", (conversation_id,)).fetchone():
            raise ConversationError("conversation_busy", "Conversation has an active turn.")
        con.execute("DELETE FROM conversations WHERE conversation_id=? AND owner_id=?", (conversation_id, owner_id))


def history(db_path: Path, owner_id: str, conversation_id: str, *, limit=100, before=None) -> list[dict]:
    with connection(db_path) as con:
        _owned(con, owner_id, conversation_id)
        rows = con.execute("""SELECT * FROM messages WHERE conversation_id=? AND sequence<? AND created_at>=?
                              ORDER BY sequence DESC LIMIT ?""",
                           (conversation_id, before or 2**63-1, retention.cutoff(), limit)).fetchall()
    return [{"message_id": row["message_id"], "conversation_id": conversation_id,
             "sequence": row["sequence"], "request_id": row["request_id"], "role": row["role"],
             "content": row["content"], "sources": json.loads(row["sources_json"]),
             "metadata": json.loads(row["metadata_json"]), "created_at": row["created_at"]} for row in reversed(rows)]


@dataclass(frozen=True)
class Reservation:
    token: str
    response: dict | None = None


def reserve(db_path: Path, owner_id: str, conversation_id: str, request_id: str,
            message: str, deadline: float) -> Reservation:
    digest = hashlib.sha256(message.encode()).hexdigest()
    with connection(db_path, write=True) as con:
        now = retention.utc_now()
        _owned(con, owner_id, conversation_id)
        _recover(con, conversation_id, now)
        prior = con.execute("SELECT * FROM conversation_turns WHERE conversation_id=? AND request_id=?",
                            (conversation_id, request_id)).fetchone()
        if prior:
            if prior["state"] == "completed":
                live = con.execute("""SELECT count(*), min(created_at) FROM messages
                                      WHERE conversation_id=? AND request_id=?""",
                                   (conversation_id, request_id)).fetchone()
                if prior["response_json"] is None or live[0] != 2 or live[1] < now - retention.RETENTION_SECONDS:
                    raise ConversationError("request_expired", "This request's messages expired. The request ID remains reserved.")
            elif not prior["message_hash"]:
                raise ConversationError("request_failed", "Request previously failed; use a new ID for a new attempt.")
            if prior["message_hash"] != digest:
                raise ConversationError("request_conflict", "Request ID was already used for another message.")
            if prior["state"] == "completed":
                return Reservation(prior["reservation_token"], json.loads(prior["response_json"]))
            code = "request_in_progress" if prior["state"] == "active" else "request_failed"
            raise ConversationError(code, "Request is active or previously failed; use a new ID only for a new attempt.")
        if con.execute("SELECT 1 FROM conversation_turns WHERE conversation_id=? AND state='active'", (conversation_id,)).fetchone():
            raise ConversationError("conversation_busy", "Another turn is active in this conversation.")
        token = str(uuid4())
        con.execute("INSERT INTO conversation_turns VALUES(?,?,?,?,?,?,?,NULL)",
                    (conversation_id, request_id, digest, token, "active", deadline, deadline + 5))
        return Reservation(token)


def acquire_model(db_path: Path, owner_id: str, conversation_id: str, token: str, limit: int):
    with connection(db_path, write=True) as con:
        _owned(con, owner_id, conversation_id)
        now = retention.utc_now()
        turn = con.execute("SELECT * FROM conversation_turns WHERE conversation_id=? AND reservation_token=? AND state='active' AND deadline>?",
                           (conversation_id, token, now)).fetchone()
        if not turn:
            raise ConversationError("reservation_expired", "Turn reservation expired.")
        con.execute("DELETE FROM model_work WHERE expires_at<=?", (now,))
        if con.execute("SELECT count(*) FROM model_work").fetchone()[0] >= limit:
            raise ConversationError("model_busy", "Local model capacity is busy; try a new request later.", 503)
        con.execute("INSERT INTO model_work VALUES(?,?)", (token, turn["expires_at"]))


def finish(db_path: Path, owner_id: str, conversation_id: str, request_id: str,
           token: str, message: str, result: dict, *, history_expires_at: float | None = None):
    with connection(db_path, write=True) as con:
        conversation = _owned(con, owner_id, conversation_id)
        now = retention.utc_now()
        retention.check_context(history_expires_at, now=now)
        row = con.execute("SELECT * FROM conversation_turns WHERE conversation_id=? AND request_id=? AND reservation_token=? AND state='active' AND deadline>?",
                          (conversation_id, request_id, token, now)).fetchone()
        if not row:
            raise ConversationError("reservation_expired", "Turn expired or was superseded; no history was written.")
        if row["message_hash"] != hashlib.sha256(message.encode()).hexdigest():
            raise ConversationError("request_conflict", "Final message differs from the reserved request.")
        sequence = conversation["next_sequence"]
        metadata = {"status": result["status"], "reason": result.get("reason")}
        for offset, role, content, sources, meta in (
            (0, "user", message, [], {}),
            (1, "assistant", result["answer"], result["sources"], metadata),
        ):
            con.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                        (str(uuid4()), conversation_id, sequence + offset, request_id, role,
                         content, json.dumps(sources, ensure_ascii=False), json.dumps(meta), now))
        con.execute("UPDATE conversations SET next_sequence=?, updated_at=? WHERE conversation_id=? AND owner_id=?",
                    (sequence + 2, now, conversation_id, owner_id))
        con.execute("UPDATE conversation_turns SET state='completed', response_json=? WHERE reservation_token=?",
                    (json.dumps(result, ensure_ascii=False), token))
        con.execute("DELETE FROM model_work WHERE reservation_token=?", (token,))


def abort(db_path: Path, owner_id: str, conversation_id: str, token: str):
    with connection(db_path, write=True) as con:
        _owned(con, owner_id, conversation_id)
        con.execute("UPDATE conversation_turns SET state='failed' WHERE conversation_id=? AND reservation_token=? AND state='active'",
                    (conversation_id, token))
        con.execute("DELETE FROM model_work WHERE reservation_token=?", (token,))
