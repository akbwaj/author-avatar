"""Citation and literal quotation integrity, not semantic evidence review."""

import re

from pydantic import BaseModel, ConfigDict

from app.retrieval.search import SearchResult

from .models import GenerationResult
from .provider import InvalidModelOutput

MARKER = re.compile(r"\[\[([^\[\]\n]+)\]\]")
QUOTATION = re.compile(r'"([^"\n]+)"|“([^“”]+)”')


class ResolvedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passage_id: str
    source_id: str
    edition_id: str
    section_id: str
    section_key: str
    section_kind: str
    chapter_number: int | None
    chapter_title: str
    passage_kind: str
    source_url: str | None
    edition_status: str | None
    html_anchor: str | None
    excerpt: str


def validate_and_resolve(result: GenerationResult, supplied: list[SearchResult]) -> list[ResolvedSource]:
    canonical = {passage.passage_id: passage for passage in supplied}
    markers = set(MARKER.findall(result.answer))
    # Reject broken/alternate bracket syntax rather than overlooking a marker.
    remainder = MARKER.sub("", result.answer)
    if "[" in remainder or "]" in remainder:
        raise InvalidModelOutput("Malformed citation marker.")
    if markers != set(result.citations):
        raise InvalidModelOutput("Citation markers and citation IDs disagree.")
    if any(citation not in canonical for citation in result.citations):
        raise InvalidModelOutput("Citation references evidence not supplied to the model.")
    # Check literal double-quoted spans against the cited stored passages.
    # This is deliberately narrower than validating factual claim support.
    quoted_remainder = QUOTATION.sub("", result.answer)
    if any(mark in quoted_remainder for mark in ('"', '“', '”')):
        raise InvalidModelOutput("Malformed quotation.")
    evidence = [" ".join(canonical[pid].passage_text.split()) for pid in result.citations]
    for match in QUOTATION.finditer(result.answer):
        quotation = " ".join((match.group(1) or match.group(2)).split())
        if not any(quotation in text for text in evidence):
            raise InvalidModelOutput("Quotation not found in cited stored evidence.")
    return [ResolvedSource(
        passage_id=passage.passage_id,
        source_id=passage.source_id,
        edition_id=passage.edition_id,
        section_id=passage.section_id,
        section_key=passage.section_key,
        section_kind=passage.section_kind,
        chapter_number=passage.chapter_number,
        chapter_title=passage.chapter_title,
        passage_kind=passage.passage_kind,
        source_url=passage.source_url,
        edition_status=passage.edition_status,
        html_anchor=passage.html_anchor,
        excerpt=passage.passage_text[:600],
    ) for passage in (canonical[pid] for pid in result.citations)]
