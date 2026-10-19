"""Opt-in live local-model test: python -m scripts.smoke_ollama.

Uses the real read-only book retrieval and the same answer/citation validation
as POST /chat. Never invoked by pytest; never downloads a model or retries.
"""

import argparse
import asyncio
import os
from pathlib import Path
import sys
from uuid import uuid4

from app.generation.provider import ModelSettings, ProviderError
from app.orchestration.chat import answer_question
from app.retrieval.index import SearchIndexError
from app.retrieval.search import keyword_query


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/author_avatar.sqlite3")))
    parser.add_argument("--question", default="What is Kriya Yoga?")
    args = parser.parse_args()
    try:
        keyword_query(args.question)
        settings = ModelSettings.from_environment()
        if settings.provider != "ollama":
            parser.error("Set LLM_PROVIDER=ollama and OLLAMA_MODEL to an installed local model.")
        result = asyncio.run(answer_question(args.question, args.db, str(uuid4()), settings=settings))
    except (ProviderError, SearchIndexError, ValueError) as error:
        # Application errors contain safe messages, never upstream bodies.
        print(f"Live smoke test failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    if result.status != "answered":
        print("Live model abstained; no grounded answer was demonstrated.", file=sys.stderr)
        return 2
    print("Live answer passed application schema and citation/quotation integrity checks.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
