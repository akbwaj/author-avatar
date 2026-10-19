"""Reserve -> bounded work -> atomic final history, with one shared deadline."""
import asyncio
from pathlib import Path

from app.generation.models import AI_IDENTITY
from app.generation.provider import (
    ModelProvider, ModelSettings, ProviderError, ProviderTimeout, ProviderBusy, create_provider,
)
from app.generation.rewrite import rewrite_question
from app.memory.context import MemorySettings, bounded_history, completed_pairs, needs_missing_context, has_reference
from app.memory import storage, retention
from app.retrieval.index import SearchIndexError
from .chat import MAX_ANSWER_CALLS, ChatResponse, answer_question


class TurnProvider:
    """Count rewriting, authoring, review and one revision; no automatic retry."""
    def __init__(self, db, owner, conversation_id, token, limit, settings, provider=None, *, history_expires_at=None):
        self.db, self.owner, self.conversation_id, self.token = db, owner, conversation_id, token
        self.limit, self.settings, self.provider = limit, settings, provider
        self.calls = 0
        self.history_expires_at = history_expires_at

    async def generate(self, request):
        retention.check_context(self.history_expires_at)
        if self.calls >= MAX_ANSWER_CALLS + 1:
            raise ProviderError("Conversation model-call budget exceeded.")
        if self.calls == 0:
            try:
                storage.acquire_model(self.db, self.owner, self.conversation_id, self.token, self.limit)
            except storage.ConversationError as error:
                if error.code == "model_busy":
                    raise ProviderBusy("Local model capacity is busy.") from None
                if error.code == "reservation_expired":
                    raise ProviderTimeout("Conversation deadline exceeded.") from None
                raise
        self.calls += 1
        if self.provider is None:
            self.provider = create_provider(self.settings)
        # Acquiring a SQLite lease/configuring a provider may have taken time.
        # Recheck immediately before handing history to the underlying model.
        retention.check_context(self.history_expires_at)
        return await self.provider.generate(request)


def clarification(message: str, request_id: str) -> ChatResponse:
    return ChatResponse(status="clarification_needed", answer=f"{AI_IDENTITY}\n\n{message}",
                        sources=[], request_id=request_id, reason="The follow-up reference is ambiguous.")


async def conversation_answer(
    message: str, db: Path, owner: str, conversation_id: str, request_id: str, *,
    provider: ModelProvider | None = None,
) -> dict:
    start = asyncio.get_running_loop().time()
    config = MemorySettings.from_environment()
    deadline = retention.utc_now() + config.deadline_seconds
    reservation = storage.reserve(db, owner, conversation_id, request_id, message, deadline)
    if reservation.response is not None:
        return reservation.response
    history_expires_at = None

    async def work():
        nonlocal history_expires_at
        rows = storage.history(db, owner, conversation_id)
        context = bounded_history(rows, config.history_bytes)
        selected_sequences = {row["sequence"] for row in context}
        history_expires_at = min((row["created_at"] + retention.RETENTION_SECONDS
                                  for row in rows if row["sequence"] in selected_sequences), default=None)
        has_history = any(completed_pairs(rows))
        settings = None
        turn_provider = None
        search_question = message
        if not has_history and needs_missing_context(message):
            result = clarification("Which person or topic are you referring to?", request_id)
        elif has_history or has_reference(message):
            settings = ModelSettings.from_environment()
            turn_provider = TurnProvider(db, owner, conversation_id, reservation.token,
                                         config.concurrent_turns, settings, provider,
                                         history_expires_at=history_expires_at)
            rewrite = await rewrite_question(message, context, turn_provider, settings)
            if rewrite.status == "clarification_needed":
                result = clarification(rewrite.clarification, request_id)
            else:
                search_question = rewrite.search_question
                result = await answer_question(message, db, request_id, provider=turn_provider,
                                               settings=settings, search_question=search_question, history=context)
        else:
            # Preserve lazy model configuration on empty retrieval. The factory
            # is injected only when actual generation is required.
            from . import chat as author

            class LazyTurnProvider:
                async def generate(self, request):
                    nonlocal turn_provider
                    if turn_provider is None:
                        settings = ModelSettings.from_environment()
                        turn_provider = TurnProvider(db, owner, conversation_id, reservation.token,
                                                     config.concurrent_turns, settings, provider)
                        # Reuse the author factory (including offline test injection).
                        turn_provider.provider = provider if provider is not None else author.create_provider(settings)
                    return await turn_provider.generate(request)

            result = await answer_question(message, db, request_id, provider=LazyTurnProvider())
        body = {**result.model_dump(), "conversation_id": conversation_id}
        storage.finish(db, owner, conversation_id, request_id, reservation.token, message, body,
                       history_expires_at=history_expires_at)
        return body

    try:
        remaining = config.deadline_seconds - (asyncio.get_running_loop().time() - start)
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(work(), timeout=remaining)
    except asyncio.TimeoutError:
        raise ProviderTimeout("Conversation deadline exceeded.") from None
    except storage.ConversationError as error:
        if error.code == "reservation_expired" and retention.utc_now() >= deadline:
            raise ProviderTimeout("Conversation deadline exceeded.") from None
        raise
    except (ProviderError, SearchIndexError):
        # The author layer deliberately sanitizes unexpected provider failures.
        # Retain the coordinator's specific expiry response if a model-call
        # boundary rejected the now-expired snapshot inside that layer.
        retention.check_context(history_expires_at)
        raise
    except Exception:
        raise ProviderError("Conversation processing failed.") from None
    finally:
        # Completed turns are untouched; failures/cancellation release capacity.
        # If cleanup itself fails, expiry still fences out late completions.
        try:
            storage.abort(db, owner, conversation_id, reservation.token)
        except storage.ConversationError:
            pass
