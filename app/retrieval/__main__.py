"""Index/search CLI: python -m app.retrieval {index,search}."""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from .index import SearchIndexError, rebuild_index
from .search import search_passages


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline SQLite FTS5 keyword retrieval")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("index", "search"):
        command = commands.add_parser(name)
        command.add_argument("--db", type=Path,
                             default=Path(os.getenv("DATABASE_PATH", "data/author_avatar.sqlite3")))
        if name == "search":
            command.add_argument("--question", required=True)
            command.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.command == "index":
            count = rebuild_index(args.db)
            print(f"Indexed {count} passages in {args.db}")
        else:
            results = search_passages(args.db, args.question, args.top_k)
            print(json.dumps({"results": [asdict(result) for result in results]},
                             ensure_ascii=False, indent=2))
    except (ValueError, SearchIndexError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
