"""Application-storage retention: five days means exactly 120 hours in UTC."""
import time
from pathlib import Path

RETENTION_SECONDS = 120 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60
CLEANUP_BATCH_SIZE = 200


def utc_now() -> float:
    """Server-generated UTC Unix seconds; replace this clock in tests."""
    return time.time()


def cutoff() -> float:
    return utc_now() - RETENTION_SECONDS


def check_context(expires_at: float | None, *, now: float | None = None) -> None:
    """Fence late generations that used history which has since expired."""
    from .storage import ConversationError

    if expires_at is not None and (utc_now() if now is None else now) > expires_at:
        raise ConversationError("history_expired", "History expired during this turn. Submit a new request ID.")


def cleanup(db_path: Path) -> dict[str, int]:
    """Idempotently erase expired rows and replay copies; keep ID tombstones.

    Bounded write batches serialize with reservation/finalization. Neither active
    turn state nor model leases are changed. There are no stored summaries in
    this version; a future summary store must participate in this deletion.
    """
    from .storage import connection

    boundary = cutoff()
    deleted = caches = 0
    while True:
        with connection(db_path, write=True) as con:
            rows = con.execute("""SELECT message_id,conversation_id,request_id FROM messages
                                  WHERE created_at<? ORDER BY created_at LIMIT ?""",
                               (boundary, CLEANUP_BATCH_SIZE)).fetchall()
            # Invalidate the whole replay copy if either message expires, while
            # independently preserving each newer message in the same request.
            for cid, rid in {(row["conversation_id"], row["request_id"]) for row in rows}:
                caches += con.execute("""UPDATE conversation_turns SET response_json=NULL,message_hash=''
                                         WHERE conversation_id=? AND request_id=? AND state='completed'
                                           AND (response_json IS NOT NULL OR message_hash<>'')""", (cid, rid)).rowcount
            con.executemany("DELETE FROM messages WHERE message_id=?", [(row["message_id"],) for row in rows])
            deleted += len(rows)
        if len(rows) < CLEANUP_BATCH_SIZE:
            break

    # Also erase orphaned replay copies left by older/manual deletions. The
    # tombstone itself stays, so absence of messages can never authorize replay.
    while True:
        with connection(db_path, write=True) as con:
            count = con.execute("""UPDATE conversation_turns SET response_json=NULL,message_hash=''
                WHERE rowid IN (SELECT t.rowid FROM conversation_turns t
                    WHERE state='completed' AND (response_json IS NOT NULL OR message_hash<>'')
                      AND (SELECT count(*) FROM messages m WHERE m.conversation_id=t.conversation_id
                           AND m.request_id=t.request_id) <> 2 LIMIT ?)""", (CLEANUP_BATCH_SIZE,)).rowcount
            caches += count
        if count < CLEANUP_BATCH_SIZE:
            break
    while True:
        with connection(db_path, write=True) as con:
            count = con.execute("""UPDATE conversation_turns SET message_hash='' WHERE rowid IN (
                SELECT rowid FROM conversation_turns WHERE state IN ('failed','expired')
                AND message_hash<>'' AND expires_at<? LIMIT ?)""", (boundary, CLEANUP_BATCH_SIZE)).rowcount
        if count < CLEANUP_BATCH_SIZE:
            break
    return {"messages_deleted": deleted, "response_caches_deleted": caches}
