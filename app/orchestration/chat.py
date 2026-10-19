"""Retrieve, generate once, then validate citations before releasing an answer."""

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from app.generation.citations import ResolvedSource, validate_and_resolve
from app.generation.models import AI_IDENTITY, MAX_OUTPUT_CHARACTERS, GenerationResult
from app.generation.persona import MAX_PASSAGES, build_request
from app.generation.provider import (
    InvalidModelOutput, ModelProvider, ModelSettings, ProviderError, ProviderTimeout, create_provider,
)
from app.retrieval.search import search_passages


class ChatResponse(BaseModel):
    status: Literal["answered", "insufficient_evidence", "clarification_needed"]
    answer: str
    sources: list[ResolvedSource]
    request_id: str
    reason: str | None = None


def abstain(reason: str, request_id: str) -> ChatResponse:
    return ChatResponse(
        status="insufficient_evidence",
        answer=f"{AI_IDENTITY}\n\nI do not have sufficient retrieved evidence to answer this question.",
        sources=[], request_id=request_id, reason=reason,
    )


async def answer_question(
    question: str, db_path: Path, request_id: str, *,
    provider: ModelProvider | None = None, settings: ModelSettings | None = None,
    search_question: str | None = None, history: list[dict] | None = None,
) -> ChatResponse:
    # SQLite opens and closes its connection in the worker, not on the event loop.
    retrieved = await run_in_threadpool(search_passages, db_path, search_question or question, MAX_PASSAGES)
    if not retrieved:
        return abstain("No passages matched the question.", request_id)
    # Empty retrieval never configures or contacts a provider.
    settings = settings or ModelSettings.from_environment()
    request, supplied = build_request(question, retrieved, max_prompt_bytes=settings.max_prompt_bytes, history=history)
    if not supplied:
        return abstain("No whole retrieved passage fits the context budget.", request_id)
    # Do not construct a provider when no whole passage fits.
    provider = provider if provider is not None else create_provider(settings)
    try:
        raw = await asyncio.wait_for(provider.generate(request), timeout=settings.timeout_seconds)
    except asyncio.TimeoutError:
        raise ProviderTimeout("Generation timed out.") from None
    except ProviderError:
        raise
    except Exception:
        # Do not propagate SDK errors that could contain a key or full prompt.
        raise ProviderError("Generation provider failed.") from None
    if not isinstance(raw, str) or len(raw) > MAX_OUTPUT_CHARACTERS:
        raise InvalidModelOutput("Invalid model response size or type.")
    try:
        result = GenerationResult.model_validate_json(raw)
    except (ValidationError, ValueError):
        # Pydantic errors can include the full input. Never expose them to HTTP/logs.
        raise InvalidModelOutput("Model response did not match the required schema.") from None
    sources = validate_and_resolve(result, supplied)
    return ChatResponse(
        status=result.status, answer=f"{AI_IDENTITY}\n\n{result.answer}",
        sources=sources, reason=result.reason, request_id=request_id,
    )
