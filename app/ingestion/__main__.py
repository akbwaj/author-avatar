"""Run with python -m app.ingestion --source ... --db ... --report ..."""

import argparse
from pathlib import Path

from .parser import parse_book
from .passages import split_passages
from .report import inspection_report
from .storage import store_book


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest inspected Gutenberg HTML into SQLite, offline")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("data/author_avatar.sqlite3"))
    parser.add_argument("--report", type=Path, default=Path("reports/ingestion-inspection.md"))
    parser.add_argument("--max-words", type=int, default=250)
    parser.add_argument("--edition-id", help="Explicit edition identifier, after manual verification")
    args = parser.parse_args()
    if args.max_words < 1:
        parser.error("--max-words must be positive")
    paths = [p.resolve() for p in (args.source, args.db, args.report)]
    if len(set(paths)) != 3:
        parser.error("source, database, and report must be distinct paths")
    try:
        book = parse_book(args.source, args.edition_id)
        passages = split_passages(book, args.max_words)
        report = inspection_report(book, passages, args.max_words)
        store_book(args.db, book, passages, args.max_words)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Ingestion failed: {error}\n")
    print(f"Stored {len(book.sections)} sections and {len(passages)} passages in {args.db}")
    print(f"Inspection report: {args.report}; warnings: {len(book.warnings)}")


if __name__ == "__main__":
    main()
