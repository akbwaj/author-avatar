"""Inspection artifact generated from the parsed source, never model output."""

from collections import Counter

from .parser import Book
from .passages import Passage


def inspection_report(book: Book, passages: tuple[Passage, ...], max_words: int) -> str:
    counts = Counter(p.section_key for p in passages)
    lines = ["# Book ingestion inspection report", "",
        f"Source: `{book.metadata['local_path']}`",
        f"SHA-256: `{book.metadata['sha256']}`",
        f"Source ID: `{book.source_id}`", f"Edition ID: `{book.edition_id}`",
        f"Copyright statement: {book.metadata['copyright_statement']}",
        f"Edition status: {book.metadata['edition_status']}",
        f"Source URL (from file): {book.metadata['source_url']}",
        f"Parser: `{book.metadata['parser_version']}`; maximum words: {max_words}",
        f"Sections: {len(book.sections)}; chapters: {sum(s.kind == 'chapter' for s in book.sections)}; passages: {len(passages)}",
        f"Explicit repeated running headers/footers removed: {book.metadata['removed_running_headers_or_footers']}",
        "", "No page numbers are inferred. Preface authorship is separate from the author's narrative.",
        "", "## Section inventory", "",
        "| Section | Body title | Passages | Body / footnote / caption / verse blocks |",
        "| --- | --- | ---: | --- |"]
    for section in book.sections:
        kinds = Counter(b.kind for b in section.blocks)
        title = section.title.replace("|", "\\|")
        lines.append(f"| {section.key} | {title} | {counts[section.key]} | " +
                     " / ".join(str(kinds[k]) for k in ("body", "footnote", "caption", "verse")) + " |")
    lines.extend(["", "## Empty or suspicious sections and metadata", ""])
    lines.extend([f"- {warning}" for warning in book.warnings] or ["- None detected by structural checks."])
    lines.extend([("- Introduction: preserved as a separate section." if any(s.kind == "introduction" for s in book.sections)
                   else "- Introduction: not present in this source; no section was invented."),
        "- Copyright year is source evidence, not proof of a particular print edition.",
        "- File metadata modification date and visible Gutenberg update date may differ; both concern the digital source.",
        "", "## Contents titles differing from body headings", ""])
    for section in book.sections:
        if section.toc_title and section.toc_title.casefold() != section.title.casefold():
            lines.append(f"- {section.key}: contents **{section.toc_title}**; body **{section.title}**.")
    lines.extend(["", "## Beginning, middle, and end samples within every section", "",
        "Samples are actual stored passage text. Long samples show only the first 400 characters, explicitly labelled.", ""])
    for section in book.sections:
        group = [p for p in passages if p.section_key == section.key]
        lines.extend([f"### {section.key}: {section.title}", ""])
        if not group:
            lines.extend(["EMPTY: no passages.", ""])
            continue
        for label, index in (("Beginning", 0), ("Middle", len(group) // 2), ("End", len(group) - 1)):
            p = group[index]
            excerpt = p.text[:400]
            lines.extend([f"**{label}** — passage {p.ordinal}, `{p.kind}`, blocks {p.block_start}–{p.block_end}",
                          f"ID: `{p.passage_id}`", ""])
            lines.extend("> " + line for line in excerpt.splitlines())
            if len(p.text) > 400:
                lines.append("\n*Excerpt truncated after 400 characters; inspect full text in SQLite.*")
            lines.append("")
    lines.extend(["## Manual comparison checklist", "",
        "- Open the original HTML locally; follow the contents links to chapters 1, 24, and 48, then spot-check the other chapter starts and ends.",
        "- Compare all 48 body titles and boundaries; contents wording can differ legitimately.",
        "- Confirm Evans-Wentz's preface and the author's acknowledgments are separate, and confirm there is no introduction.",
        "- Check that contents, illustration list, title/copyright/dedication pages, and Gutenberg boilerplate are absent from passages.",
        "- Check chapter 1 and preface footnote 1-1: the same numeric prefix does not determine chapter attribution.",
        "- Inspect italic boundaries, inline footnote markers, hyphenated line breaks, captions, and verse (especially chapter 48's final footnote poem).",
        "- If a caption is flagged as ending with a single-letter initial, check whether this is a complete name or truncated source text.",
        "- Inspect all warnings, short sections, and repeated blocks; retained repetition is not automatically an error.",
        "- Compare full SQLite passages when a report excerpt is truncated; whitespace-normalized prose is not a facsimile transcription.",
        "- Verify the edition against an authoritative copy before assigning a stronger edition label.", ""])
    return "\n".join(lines)
