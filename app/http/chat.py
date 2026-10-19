"""Authenticated, owner-scoped conversation turn HTTP adapter."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.generation.provider import (
    InvalidModelOutput, ProviderConfigurationError, ProviderError, ProviderTimeout,
    ModelUnavailable, ProviderUnavailable, ProviderBusy,
)
from app.http.search import get_database_path
from app.http.identity import get_owner
from app.orchestration.conversation import conversation_answer
from app.memory.storage import ConversationError
from app.retrieval.index import SearchIndexError
from app.retrieval.search import MAX_QUESTION_LENGTH, keyword_query

router = APIRouter()


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    message: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH, strict=True)
    request_id: UUID

    @field_validator("message")
    @classmethod
    def validate_question(cls, value: str) -> str:
        keyword_query(value)
        return value


@router.post("/chat", description=(
    "Authenticated conversation grounded in newly retrieved Autobiography of a Yogi passages. "
    "Citation integrity is validated and answered drafts receive evidence review, with at most one revision."
), responses={502: {"description": "Provider failed or returned invalid output"},
              503: {"description": "Retrieval or provider configuration unavailable"},
              504: {"description": "Provider timeout"}})
async def chat(request: ChatRequest, db_path: Path = Depends(get_database_path), owner: str = Depends(get_owner)):
    request_id = str(request.request_id)
    try:
        result = await conversation_answer(request.message, db_path, owner, str(request.conversation_id), request_id)
    except ConversationError as error:
        status, code, message = error.status, error.code, str(error)
    except SearchIndexError:
        status, code, message = 503, "retrieval_unavailable", (
            "Search unavailable. Ingest the book and run python -m app.retrieval index."
        )
    except ProviderConfigurationError:
        status, code, message = 503, "provider_configuration_error", "Invalid server model configuration."
    except ProviderTimeout:
        status, code, message = 504, "provider_timeout", "Generation timed out."
    except ModelUnavailable:
        status, code, message = 503, "model_unavailable", "Configured model is unavailable. Install it on the model server."
    except ProviderBusy:
        status, code, message = 503, "model_busy", "Local model capacity is busy. Try a new request later."
    except ProviderUnavailable:
        status, code, message = 503, "provider_unavailable", "Generation service is unavailable. Start the configured model server."
    except InvalidModelOutput:
        status, code, message = 502, "invalid_model_output", "Model output failed validation."
    except ProviderError:
        status, code, message = 502, "provider_failure", "Generation provider failed."
    else:
        return JSONResponse(content=result, headers={"X-Request-ID": request_id})
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )
