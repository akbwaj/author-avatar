"""Owner-scoped conversation API. Identity is always derived on the server."""
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.memory import storage
from .identity import get_owner
from .search import get_database_path

router = APIRouter()


def call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except storage.ConversationError as error:
        raise HTTPException(error.status, {"code": error.code, "message": str(error)}) from None


@router.post("/conversations", status_code=201)
def create(owner: str = Depends(get_owner), db: Path = Depends(get_database_path)):
    return call(storage.create_conversation, db, owner)


@router.get("/conversations")
def listing(owner: str = Depends(get_owner), db: Path = Depends(get_database_path)):
    return {"conversations": call(storage.list_conversations, db, owner)}


@router.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: UUID, limit: int = Query(100, ge=1, le=100),
             before_sequence: int | None = Query(None, ge=1),
             owner: str = Depends(get_owner), db: Path = Depends(get_database_path)):
    return {"messages": call(storage.history, db, owner, str(conversation_id), limit=limit, before=before_sequence)}


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete(conversation_id: UUID, owner: str = Depends(get_owner), db: Path = Depends(get_database_path)):
    call(storage.delete_conversation, db, owner, str(conversation_id))
    return Response(status_code=204)
