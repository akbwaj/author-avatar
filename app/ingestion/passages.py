"""Deterministic, non-overlapping passage segmentation within sections."""

from dataclasses import dataclass
from hashlib import sha256
import json

from .parser import Book, Section


@dataclass(frozen=True)
class Passage:
    passage_id: str
    section_key: str
    ordinal: int
    kind: str
    text: str
    block_start: int
    block_end: int
    anchors: tuple[str, ...]


def section_id(book: Book, section: Section) -> str:
    return "section-" + sha256(json.dumps(
        [book.edition_id, book.source_id, section.key], ensure_ascii=False
    ).encode()).hexdigest()


def split_passages(book: Book, max_words: int = 250) -> tuple[Passage, ...]:
    if max_words < 1:
        raise ValueError("max_words must be positive")
    result = []
    for section in book.sections:
        pending = []
        pending_words = 0
        ordinal = 0

        def emit():
            nonlocal pending, pending_words, ordinal
            if not pending:
                return
            ordinal += 1
            text = "\n\n".join(part[2] for part in pending)
            kind = pending[0][1]
            identity = [section_id(book, section), ordinal, kind, text]
            passage_id = "passage-" + sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            result.append(Passage(passage_id, section.key, ordinal, kind, text,
                                  pending[0][0], pending[-1][0],
                                  tuple(dict.fromkeys(p[3] for p in pending if p[3]))))
            pending, pending_words = [], 0

        for block_index, block in enumerate(section.blocks, 1):
            words = block.text.split()
            # Only oversized blocks are split at whitespace; ordinary blocks
            # retain their normalized text and verse line breaks.
            parts = [block.text] if len(words) <= max_words else [
                " ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]
            for part in parts:
                size = len(part.split())
                if pending and (pending[0][1] != block.kind or pending_words + size > max_words):
                    emit()
                pending.append((block_index, block.kind, part, block.anchor))
                pending_words += size
        emit()
    return tuple(result)
