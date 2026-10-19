"""Retrieve once, author, review, optionally revise once and review again."""

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from app.generation.citations import ResolvedSource, validate_and_resolve
from app.generation.models import AI_IDENTITY, MAX_OUTPUT_CHARACTERS, GenerationResult
from app.generation.persona import MAX_PASSAGES, build_request
from app.generation.provider import (
    GenerationRequest, InvalidModelOutput, ModelProvider, ModelSettings, ProviderError, ProviderTimeout, create_provider,
)
from app.generation.reviewer import review_draft
from app.generation.revision import build_revision_request
from app.memory.context import MemorySettings
from app.retrieval.search import SearchResult, search_passages

MAX_ANSWER_CALLS = 4
REVIEW_HEADROOM_BYTES = 4000


class AnswerProvider:
    """Enforce the four-call author/reviewer budget, including standalone use."""
    def __init__(self, provider: ModelProvider):
        self.provider, self.calls = provider, 0

    async def generate(self, request: GenerationRequest) -> str:
        if self.calls >= MAX_ANSWER_CALLS:
            raise ProviderError("Answer model-call budget exceeded.")
        self.calls += 1
        return await self.provider.generate(request)


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
    # Conversation calls also have an outer deadline including rewriting and
    # final persistence. Standalone smoke calls get a bounded overall workflow.
    try:
        return await asyncio.wait_for(_answer_question(
            question, db_path, request_id, provider=provider, settings=settings,
            search_question=search_question, history=history,
        ), timeout=MemorySettings.from_environment().deadline_seconds)
    except asyncio.TimeoutError:
        raise ProviderTimeout("Answer workflow deadline exceeded.") from None


async def _answer_question(
    question: str, db_path: Path, request_id: str, *,
    provider: ModelProvider | None, settings: ModelSettings | None,
    search_question: str | None, history: list[dict] | None,
) -> ChatResponse:
    # SQLite opens and closes its connection in the worker, not on the event loop.
    retrieved = await run_in_threadpool(search_passages, db_path, search_question or question, MAX_PASSAGES)
    if not retrieved:
        return abstain("No passages matched the question.", request_id)
    # Empty retrieval never configures or contacts a provider.
    settings = settings or ModelSettings.from_environment()
    budget = settings.max_prompt_bytes
    # Leave input headroom for the draft and review findings. Every subsequent
    # request is checked against the full budget using its actual serialized
    # size; a very large draft/findings fails explicitly, never by dropping IDs.
    author_budget = budget - min(REVIEW_HEADROOM_BYTES, budget // 3) if budget is not None else None
    request, supplied = build_request(question, retrieved, max_prompt_bytes=author_budget, history=history)
    if not supplied:
        return abstain("No whole retrieved passage fits the context budget.", request_id)
    # Do not construct a provider when no whole passage fits.
    provider = provider if provider is not None else create_provider(settings)
    counted = AnswerProvider(provider)
    draft = await generate_answer(request, supplied, counted, settings, request_id)
    if draft.status != "answered":
        return draft
    findings = await review_draft(question, draft.answer, request, provider=counted, settings=settings)
    if findings.verdict == "pass":
        return draft
    if findings.verdict == "insufficient_evidence":
        return abstain("The supplied passages do not support a reviewed answer.", request_id)

    revision = build_revision_request(draft.answer, request, findings, max_prompt_bytes=budget)
    revised = await generate_answer(revision, supplied, counted, settings, request_id)
    if revised.status != "answered":
        # Revision saw internal findings; do not expose unreviewed prose or
        # reasons that could quote those findings or the rejected draft.
        return abstain("The supplied passages do not support a corrected answer.", request_id)
    final = await review_draft(question, revised.answer, request, provider=counted, settings=settings)
    if final.verdict != "pass":
        return abstain("A supported answer could not be confirmed after one revision.", request_id)
    return revised


async def generate_answer(request: GenerationRequest, supplied: list[SearchResult],
                          provider: ModelProvider, settings: ModelSettings, request_id: str) -> ChatResponse:
    """Apply the unchanged schema and canonical citation checks to both drafts."""
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
