"""Opt-in live local-model test: python -m scripts.smoke_ollama.

Uses read-only book retrieval and the same author/review/one-revision workflow
as POST /chat, with a shared deadline. Never invoked by pytest; never downloads
a model or retries malformed output. Does not write conversation history.
"""

import argparse
import asyncio
import os
from pathlib import Path
import sys
from uuid import uuid4

from app.generation.provider import ModelSettings, ProviderError, create_provider
from app.generation.reviewer import REVIEW_INSTRUCTIONS
from app.generation.revision import REVISION_INSTRUCTIONS
from app.orchestration.chat import answer_question
from app.retrieval.index import SearchIndexError
from app.retrieval.search import keyword_query


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/author_avatar.sqlite3")))
    parser.add_argument("--question", default="What is Kriya Yoga?")
    args = parser.parse_args()
    stages = []

    class ObservedProvider:
        """Record role names only; never log prompts, drafts or review findings."""
        def __init__(self, provider):
            self.provider = provider

        async def generate(self, request):
            role = ("review" if request.system == REVIEW_INSTRUCTIONS else
                    "revision" if request.system == REVISION_INSTRUCTIONS else "author")
            stages.append(role)
            return await self.provider.generate(request)

    try:
        keyword_query(args.question)
        settings = ModelSettings.from_environment()
        if settings.provider != "ollama":
            parser.error("Set LLM_PROVIDER=ollama and OLLAMA_MODEL to an installed local model.")
        provider = ObservedProvider(create_provider(settings))
        result = asyncio.run(answer_question(args.question, args.db, str(uuid4()), provider=provider, settings=settings))
    except (ProviderError, SearchIndexError, ValueError) as error:
        # Application errors contain safe messages, never upstream bodies.
        print("Model stages:", " -> ".join(stages) or "none", file=sys.stderr)
        print(f"Live smoke test failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Model stages:", " -> ".join(stages) or "none", file=sys.stderr)
    print(result.model_dump_json(indent=2))
    if result.status != "answered":
        print("Live model abstained; no grounded answer was demonstrated.", file=sys.stderr)
        return 2
    print("Live answer passed schema/citation checks and model evidence review; this is not proof of factual correctness.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
