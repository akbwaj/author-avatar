"""HTTP entry point for Author Avatar."""

import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI

from app.http.chat import router as chat_router
from app.http.search import router as search_router
from app.http.conversations import router as conversation_router
from app.http.search import get_database_path
from app.memory import retention, storage

logger = logging.getLogger(__name__)


async def run_retention_cleanup():
    try:
        await asyncio.to_thread(retention.cleanup, get_database_path())
    except storage.ConversationError as error:
        # Missing/unmigrated or briefly busy storage must not disable liveness.
        # Logical expiry filters still apply, even if deletion cannot run yet.
        logger.warning("Retention cleanup unavailable (%s); retrying next hour.", error.code)


async def hourly_retention_cleanup():
    while True:
        await asyncio.sleep(retention.CLEANUP_INTERVAL_SECONDS)
        await run_retention_cleanup()


@asynccontextmanager
async def lifespan(application):
    await run_retention_cleanup()
    task = asyncio.create_task(hourly_retention_cleanup())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Author Avatar", version="0.1.0", lifespan=lifespan)
app.include_router(search_router)
app.include_router(chat_router)
app.include_router(conversation_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Report process liveness without database or model-provider calls."""
    return {"status": "ok"}
