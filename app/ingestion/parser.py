"""Structural parser for the inspected Project Gutenberg HTML layout."""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, Tag

PARSER_VERSION = "gutenberg-html-v1"


@dataclass(frozen=True)
class Block:
    kind: str
    text: str
    anchor: str | None


@dataclass(frozen=True)
class Section:
    key: str
    kind: str
    chapter_number: int | None
    title: str
    toc_title: str | None
    anchor: str | None
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Book:
    source_id: str
    edition_id: str
    metadata: dict
    sections: tuple[Section, ...]
    warnings: tuple[str, ...]


def text_of(tag: Tag) -> str:
    # Existing whitespace separates inline tags; adding a separator to every
    # string would insert spaces before punctuation. BR represents a line break.
    copy = BeautifulSoup(str(tag), "html.parser")
    for br in copy.find_all("br"):
        br.replace_with("\n")
    for paragraph in copy.find_all("p"):
        paragraph.insert_before("\n")
        paragraph.insert_after("\n")
    for sup in copy.find_all("sup"):
        sup.replace_with("[" + sup.get_text() + "]")
    text = copy.get_text()
    if tag.name == "pre":
        return "\n".join(line.rstrip() for line in text.strip().splitlines())
    return " ".join(text.split())


def extract_blocks(container: Tag) -> tuple[Block, ...]:
    blocks = []
    for node in container.find_all(["p", "pre", "div"]):
        if node.name == "div" and "blockgraphic" not in node.get("class", []):
            continue
        # Some captions are bare text in DIVs, rather than paragraphs. Extract
        # the whole graphic block once, including both forms of caption.
        if any("blockgraphic" in parent.get("class", []) for parent in node.parents
               if isinstance(parent, Tag)):
            continue
        # Do not emit a nested block twice.
        if node.find_parent(["p", "pre"]) is not None:
            continue
        text = text_of(node)
        if not text:
            continue
        ancestors = [node, *node.parents]
        classes = {c for ancestor in ancestors if isinstance(ancestor, Tag)
                   for c in ancestor.get("class", [])}
        kind = "footnote" if "bookfn" in classes else (
            "caption" if "blockgraphic" in classes else (
                "verse" if node.name == "pre" else "body"))
        anchor_node = node.find(id=True)
        blocks.append(Block(kind, text, anchor_node.get("id") if anchor_node else None))
    return tuple(blocks)


def parse_book(path: Path, edition_id: str | None = None) -> Book:
    raw = path.read_bytes()
    soup = BeautifulSoup(raw.decode("utf-8-sig"), "html.parser")
    for node in soup.select("script, style"):
        node.decompose()
    body = soup.select_one(".bookbody")
    front = soup.select_one(".frontmatter")
    if body is None or front is None:
        raise ValueError("Expected Gutenberg .frontmatter and .bookbody containers")

    metadata = {str(node.get("name") or node.get("property")): node.get("content")
                for node in soup.select("meta[name], meta[property]")}
    copyright_node = front.select_one(".copyright")
    copyright_text = text_of(copyright_node) if copyright_node else "Not stated"
    metadata.update({
        "local_path": str(path.resolve()), "sha256": sha256(raw).hexdigest(),
        "parser_version": PARSER_VERSION, "copyright_statement": copyright_text,
        "edition_status": "User supplied identifier" if edition_id else
            "Edition not independently verified; identifier derives from copyright statement",
        "source_url": metadata.get("og:url") or metadata.get("dcterms.source"),
    })
    machine_header = soup.select_one("#pg-machine-header")
    metadata["gutenberg_machine_header"] = text_of(machine_header) if machine_header else None
    edition_id = edition_id or "edition-" + sha256(copyright_text.encode()).hexdigest()

    toc = {}
    for link in front.select(".toc a[href]"):
        fragment = urlsplit(link["href"]).fragment
        if re.fullmatch(r"chap[1-9]\d*", fragment):
            if fragment in toc:
                raise ValueError(f"Duplicate contents entry: {fragment}")
            toc[fragment] = text_of(link)

    containers = front.select("div.preface, div.introduction") + body.select("div.chapter")
    warnings = []
    # Only remove identical, explicitly marked running headers/footers occurring
    # in at least two DIFFERENT sections. Ordinary repeated paragraphs survive.
    running_counts = Counter()
    for container in containers:
        running_counts.update({text_of(n) for n in container.select(
            "header.running-header, footer.running-footer") if text_of(n)})
    removed = 0
    for container in containers:
        for node in container.select("header.running-header, footer.running-footer"):
            if running_counts[text_of(node)] >= 2:
                node.decompose()
                removed += 1
    metadata["removed_running_headers_or_footers"] = removed

    sections = []
    for container in containers:
        heading = container.select_one(".chaptitle h3") if "chapter" in container.get("class", []) else container.select_one("h2, .ph3")
        if heading is None:
            raise ValueError("Section has no supported heading; inspect the HTML layout")
        title = text_of(heading)
        anchor_node = container.find(id=re.compile(r"^(chap\d+|preface|introduction)$"))
        anchor = anchor_node.get("id") if anchor_node else None
        if "chapter" in container.get("class", []):
            number_heading = container.select_one(".chapnumber h2")
            match = re.fullmatch(r"CHAPTER:\s*(\d+)", text_of(number_heading)) if number_heading else None
            if not match or anchor != f"chap{int(match[1])}":
                raise ValueError("Chapter number and anchor disagree or are missing")
            number = int(match[1])
            kind, key = "chapter", f"chapter-{number:02d}"
            toc_title = toc.get(anchor)
            if toc_title is None:
                raise ValueError(f"Missing contents entry for {anchor}")
            if title.casefold() != toc_title.casefold():
                warnings.append(f"{key}: contents title differs from body heading")
        else:
            number, toc_title = None, None
            kind = "acknowledgments" if "acknowledgment" in title.casefold() else (
                "introduction" if "introduction" in title.casefold() else "preface")
            key = kind
        sections.append(Section(key, kind, number, title, toc_title, anchor,
                                extract_blocks(container)))
        unhandled = []
        for string in container.find_all(string=True):
            if isinstance(string, Comment) or not string.strip():
                continue
            if any(parent.name in {"p", "pre", "h2", "h3"} or
                   {"blockgraphic", "ph3"}.intersection(parent.get("class", []))
                   for parent in string.parents if isinstance(parent, Tag)):
                continue
            unhandled.append(str(string).strip())
        if unhandled:
            warnings.append(f"{key}: text outside supported blocks/headings, not ingested: {unhandled!r}")

    numbers = [s.chapter_number for s in sections if s.kind == "chapter"]
    if not numbers or numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("Chapter numbers must be unique, contiguous, and in document order")
    if {f"chap{n}" for n in numbers} != set(toc):
        raise ValueError("Contents and body chapter sets disagree")
    if len({s.key for s in sections}) != len(sections):
        raise ValueError("Duplicate front-matter section; inspect before assigning identifiers")
    for s in sections:
        if not s.blocks:
            warnings.append(f"{s.key}: EMPTY section")
        elif sum(len(b.text.split()) for b in s.blocks) < 100:
            warnings.append(f"{s.key}: short section (fewer than 100 words)")
        duplicates = [text for text, count in Counter(b.text for b in s.blocks).items()
                      if count > 1 and len(text) > 100]
        if duplicates:
            warnings.append(f"{s.key}: {len(duplicates)} repeated long blocks retained for inspection")
        for block in s.blocks:
            if block.kind == "caption" and re.search(r"\b[A-Z]\.$", block.text):
                warnings.append(f"{s.key}: caption ends with a single-letter initial; check completeness: {block.text[-100:]}")
    return Book("source-" + sha256(raw).hexdigest(), edition_id, metadata,
                tuple(sections), tuple(warnings))
