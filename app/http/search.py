"""Validation and HTTP adaptation for the keyword retrieval service."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.retrieval.index import SearchIndexError
from app.retrieval.search import MAX_QUESTION_LENGTH, SearchResult, keyword_query, search_passages

router = APIRouter()


def get_database_path() -> Path:
    # Export DATABASE_PATH in the server environment; .env is not auto-loaded.
    return Path(os.getenv("DATABASE_PATH", "data/author_avatar.sqlite3"))


class SearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH, strict=True,
                          description="Plain text keywords; requires at least one letter or number.")
    top_k: int = Field(default=5, ge=1, le=10, strict=True,
                       description="Maximum results, default five, allowed range 1–10.")

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        keyword_query(value)
        return value


class SearchResponse(BaseModel):
    results: list[SearchResult]


@router.post("/search", response_model=SearchResponse, description=(
    "Literal keyword OR search over passage text and section titles. Results are "
    "ordered by SQLite FTS5 BM25 score ascending (lower/more negative is better), "
    "then passage ID. Score is relevance, not confidence or probability."
))
def search(request: SearchRequest, db_path: Path = Depends(get_database_path)) -> SearchResponse:
    try:
        return SearchResponse(results=search_passages(db_path, request.question, request.top_k))
    except SearchIndexError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
