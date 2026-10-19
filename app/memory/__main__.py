"""Run the additive conversation migration against an existing database."""
import argparse
import os
from pathlib import Path

from .storage import ConversationError, migrate
from .retention import cleanup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/author_avatar.sqlite3")))
    parser.add_argument("--cleanup", action="store_true", help="Delete expired messages and response caches after migration.")
    args = parser.parse_args()
    try:
        migrate(args.db)
        result = cleanup(args.db) if args.cleanup else None
    except ConversationError as error:
        parser.exit(2, str(error) + "\n")
    print("Conversation schema version 2 is ready; existing book and conversation data preserved by migration.")
    if result is not None:
        print(f"Retention cleanup: {result['messages_deleted']} messages and {result['response_caches_deleted']} response caches deleted.")


if __name__ == "__main__":
    main()
