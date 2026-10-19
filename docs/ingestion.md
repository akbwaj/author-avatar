# Book ingestion

[Project overview](../README.md) · [Local chat setup](conversations.md)

Run shell commands from the repository root.

From the repository root, after installing the updated dependencies:

```sh
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m app.ingestion \
  --source 'data/raw/Autobiography of a YOGI _ Project Gutenberg.html' \
  --db data/author_avatar.sqlite3 \
  --report reports/ingestion-inspection.md
python -m pytest tests/test_ingestion.py
```

Rerun the same ingestion command to replace that source/edition's rows atomically
without accumulating duplicates. Other source/edition imports remain intact.
The generated SQLite file is ignored by Git; the Markdown report can be reviewed
and versioned. Neither command executes instructions from book text.

The ingestion package separates parsing (`parser.py`), passage segmentation
(`passages.py`), persistence (`storage.py`), reporting (`report.py`), and the CLI
(`__main__.py`). It uses Beautiful Soup with Python's built-in `html.parser`,
chosen after inspecting the actual file's structural markup. No PDF parser,
OCR, embedding library, or model SDK is involved.

### Parser assumptions and limitations

- This parser supports the inspected Gutenberg layout: `.frontmatter`,
  `.bookbody`, `.chapter`, `.chapnumber`, `.chaptitle`, `.preface`, and
  `.introduction`. It is not a generic parser for every Gutenberg book.
- Chapters are bounded by HTML containers, never by matching chapter words in
  prose. Chapter anchors, numbers, ordering, and the contents link set must
  agree or ingestion stops. The contents supplies metadata only, not passages.
- Body chapter headings are authoritative titles; contents titles are stored
  separately and differences are flagged. This source has 48 chapters, a
  preface, acknowledgments, and no introduction. Introductions are preserved
  separately if present in a supported layout. Unsupported duplicate front
  sections are rejected rather than assigned guessed identifiers.
- Paragraphs, preformatted verse, footnotes, and image captions are retained
  in document order. Footnotes and captions have their own passage kinds;
  a numeric footnote prefix never determines chapter attribution. Images and
  their alternative text are not transcribed. Title, dedication, copyright,
  contents, illustration lists, and Gutenberg boilerplate are outside passages.
  Source metadata and the copyright statement are retained separately.
- Prose whitespace is normalized; HTML entities are decoded, inline emphasis
  is removed, and superscript references become bracketed markers. Hyphenated
  line breaks are not guessed away. Verse line breaks are retained unless an
  oversized verse block must be split. Unexpected text outside supported blocks
  is flagged in the report for manual review.
- There are no detected running headers in this HTML. The parser removes only
  identical `header.running-header` or `footer.running-footer` text marked in
  two or more sections. Ordinary repeated prose is retained. Scripts and styles
  are discarded without executing them.
- Passage packing is deterministic and non-overlapping, with a default maximum
  of 250 whitespace-delimited words. It never mixes sections or block kinds.
  Oversized blocks split at word boundaries, which can split a sentence.
  `--max-words` changes this limit. Block ranges and available HTML anchors
  remain available for inspection; no page numbers are invented.
- Source IDs are SHA-256 hashes of the original file bytes. Default edition IDs
  hash the observed copyright statement, explicitly marked **unverified**;
  copyright 1946 alone does not establish a print edition. Use `--edition-id`
  only after verifying the edition. Sections have source/edition-specific IDs
  and readable keys such as `chapter-01`.
- Passage IDs hash the section ID, ordinal, kind, and normalized text. Identical
  input and settings produce identical IDs even if the file is renamed.
  Changed source bytes produce a new source version; a different chunk size
  can change passage IDs. Reruns with changed chunk sizes replace old passages
  for the same source/edition, so there are no stale chunks.

SQLite stores `sources`, `sections`, and `passages`. Foreign keys link each
passage through its section to a source and edition. Uniqueness constraints
prevent duplicate identifiers and passage ordinals. An import is a transaction:
if inserting passages fails, existing source data is restored by rollback.
The report is written after the database commit; if report writing fails, the
database may already be updated and rerunning is safe.

Read [the generated inspection report](../reports/ingestion-inspection.md) for every
chapter title, passage count, warnings, and beginning/middle/end samples from
each section. Compare its checklist against the original HTML, especially
chapter boundaries, the preface, footnotes, captions, and poems. Samples are
labelled excerpts, not full passages or a claim of complete editorial verification.

Implementation references: [FastAPI first steps](https://fastapi.tiangolo.com/tutorial/first-steps/)
and [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/).
HTML parsing reference: [Beautiful Soup documentation](https://www.crummy.com/software/BeautifulSoup/bs4/doc/).
