from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from app.ingestion.parser import parse_book
from app.ingestion.passages import split_passages
from app.ingestion.report import inspection_report
from app.ingestion.storage import store_book

SOURCE = Path(__file__).resolve().parents[1] / "data/raw/Autobiography of a YOGI _ Project Gutenberg.html"


@pytest.fixture(scope="module")
def real_book():
    return parse_book(SOURCE)


def snapshot(db):
    with sqlite3.connect(db) as connection:
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("sources", "sections", "passages")}


def test_real_book_chapter_attribution_and_text_preservation(real_book, tmp_path):
    book = real_book
    chapters = [s for s in book.sections if s.kind == "chapter"]
    assert [s.chapter_number for s in chapters] == list(range(1, 49))
    assert [s.kind for s in book.sections[:2]] == ["preface", "acknowledgments"]
    assert chapters[0].title == "My Parents and Early Life"
    assert chapters[23].title == "I Become A Monk Of The Swami Order"
    assert chapters[-1].title == "At Encinitas In California"
    assert chapters[0].blocks[0].text.startswith("The characteristic features of Indian culture")
    assert "My Mother’s Death" in chapters[1].title
    assert "Lord," in [b.text for b in chapters[-1].blocks if b.kind == "body"][-1]
    assert "large family!" in [b.text for b in chapters[-1].blocks if b.kind == "body"][-1]
    passages = split_passages(book)
    for section in book.sections:
        group = [p for p in passages if p.section_key == section.key]
        assert group, section.key
        assert [p.ordinal for p in group] == list(range(1, len(group) + 1))
        assert [word for p in group for word in p.text.split()] == [
            word for block in section.blocks for word in block.text.split()]
        assert all(len(p.text.split()) <= 250 for p in group)
    all_text = "\n".join(p.text for p in passages)
    assert "THE FULL PROJECT GUTENBERG" not in all_text
    assert "Most recently updated:" not in all_text
    assert "Speakers at a 1945 Interracial Meeting" in all_text
    assert "FATHER Bhagabati Charan Ghosh A Disciple of Lahiri Mahasaya" in all_text
    assert "MASTER MAHASAYA Ever engrossed in his blissful cosmic romance." in all_text
    assert "O God beautiful!" in all_text
    # Preface's FN1-1 belongs to the preface, despite the chapter-like prefix.
    assert any(p.section_key == "preface" and "Oxford University Press, 1935." in p.text
               and p.kind == "footnote" for p in passages)
    assert not any(p.section_key == "chapter-01" and "Oxford University Press, 1935." in p.text
                   for p in passages)
    db = tmp_path / "book.sqlite3"
    store_book(db, book, passages, 250)
    with sqlite3.connect(db) as connection:
        rows = connection.execute("""SELECT p.passage_id, s.section_key, s.chapter_number,
            s.source_id, s.edition_id FROM passages p JOIN sections s USING(section_id)""").fetchall()
    assert len(rows) == len(passages)
    expected = {s.key: s.chapter_number for s in book.sections}
    assert all(number == expected[key] and source == book.source_id and edition == book.edition_id
               for _, key, number, source, edition in rows)


def test_repeat_ingestion_is_identical_and_ids_survive_path_change(real_book, tmp_path):
    db = tmp_path / "book.sqlite3"
    passages = split_passages(real_book)
    store_book(db, real_book, passages, 250)
    before = snapshot(db)
    store_book(db, parse_book(SOURCE), split_passages(parse_book(SOURCE)), 250)
    assert snapshot(db) == before
    copied = tmp_path / "renamed.html"
    copied.write_bytes(SOURCE.read_bytes())
    moved_book = parse_book(copied)
    assert moved_book.source_id == real_book.source_id
    assert split_passages(moved_book) == passages
    assert len({p.passage_id for p in passages}) == len(passages)


def test_rechunking_replaces_old_rows_and_failed_import_rolls_back(real_book, tmp_path):
    db = tmp_path / "book.sqlite3"
    store_book(db, real_book, split_passages(real_book, 250), 250)
    smaller = split_passages(real_book, 100)
    store_book(db, real_book, smaller, 100)
    before = snapshot(db)
    assert len(before["passages"]) == len(smaller)
    with pytest.raises(sqlite3.IntegrityError):
        store_book(db, real_book, (*smaller, smaller[0]), 100)
    assert snapshot(db) == before
    other = replace(real_book, edition_id="verified-test-edition")
    store_book(db, other, split_passages(other, 100), 100)
    assert len(snapshot(db)["sources"]) == 2
    assert len(snapshot(db)["passages"]) == 2 * len(smaller)


def write_fixture(tmp_path, *, second_number=2, second_anchor="chap2"):
    path = tmp_path / "fixture.html"
    path.write_text(f"""<html><div class="frontmatter">
      <div class="copyright"><p>Copyright, 1946</p></div>
      <div class="toc"><a href="#chap1">First</a><a href="#chap2">Second</a></div>
      <div class="preface"><a id="preface"></a><h2>PREFACE</h2><p>Editor speaks.</p></div>
      <div class="introduction"><a id="introduction"></a><h2>INTRODUCTION</h2><p>Intro text.</p></div>
      </div><div class="bookbody">
      <div class="chapter"><div class="chapnumber"><a id="chap1"></a><h2>CHAPTER: 1</h2></div>
      <div class="chaptitle"><h3>First</h3></div>
      <header class="running-header"><p>Repeated book header</p></header>
      <p>Repeated prose stays.</p><p>Repeated prose stays.</p>
      <p>Read <i>this</i>, please.<a href="#FN1-2"><sup>1-2</sup></a></p>
      <div class="blockgraphic"><a id="fig1"></a>Bare caption<br><i>continued</i><p>More caption.</p></div>
      <div class="bookfn"><p><a id="FN1-2"></a>1-2: A note.</p></div></div>
      <div class="chapter"><div class="chapnumber"><a id="{second_anchor}"></a><h2>CHAPTER: {second_number}</h2></div>
      <div class="chaptitle"><h3>Second</h3></div>
      <header class="running-header"><p>Repeated book header</p></header>
      <p>Second chapter only.</p><div class="poem"><pre>First line\nSecond line</pre></div>
      </div></div></html>""", encoding="utf-8")
    return path


def test_front_matter_toc_repetition_inline_text_and_verse(tmp_path):
    book = parse_book(write_fixture(tmp_path))
    assert [s.kind for s in book.sections] == ["preface", "introduction", "chapter", "chapter"]
    passages = split_passages(book, 20)
    text = "\n".join(p.text for p in passages)
    assert "Repeated book header" not in text
    assert text.count("Repeated prose stays.") == 2
    assert "Read this, please.[1-2]" in text
    assert "First line\nSecond line" in text
    assert text.count("Bare caption continued More caption.") == 1
    assert any(p.kind == "caption" and "fig1" in p.anchors for p in passages)
    assert book.metadata["removed_running_headers_or_footers"] == 2
    assert all("Second chapter only." not in p.text for p in passages if p.section_key == "chapter-01")
    report = inspection_report(book, passages, 20)
    assert "Introduction: preserved" in report
    assert all(label in report for label in ("Beginning", "Middle", "End"))


@pytest.mark.parametrize("number, anchor", [(3, "chap3"), (1, "chap1"), (2, "chap9")])
def test_ambiguous_chapter_boundaries_are_rejected(tmp_path, number, anchor):
    with pytest.raises(ValueError):
        parse_book(write_fixture(tmp_path, second_number=number, second_anchor=anchor))


def test_empty_sections_are_reported_and_bad_passage_size_rejected(tmp_path):
    path = write_fixture(tmp_path)
    path.write_text(path.read_text().replace("<p>Intro text.</p>", ""), encoding="utf-8")
    book = parse_book(path)
    assert "introduction: EMPTY section" in book.warnings
    assert "EMPTY: no passages." in inspection_report(book, split_passages(book), 250)
    with pytest.raises(ValueError, match="positive"):
        split_passages(book, 0)
