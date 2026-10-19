# Book source and local databases

`raw/Autobiography of a YOGI _ Project Gutenberg.html` is the inspected source
used by offline ingestion and its regression tests. It is retained in the
repository so a fresh clone can run the full test suite without downloading a
book or contacting a model.

- Book: *Autobiography of a Yogi*, Paramahansa Yogananda.
- Project Gutenberg record: <https://www.gutenberg.org/ebooks/7452>.
- Download location recorded in the saved HTML:
  <https://www.gutenberg.org/cache/epub/7452/pg7452-images.html>.
- SHA-256: `893062ff44b2e4e30403cdb5fdbd84fe73144924e04efa50f2e19ab2e3e78d06`.
- The saved HTML contains the original Project Gutenberg header, credits, and
  full license. Those notices remain intact. See that license for the source's
  terms; a future application-code license does not replace it.
- The print edition has not been independently verified. The parser records
  that limitation instead of inventing an edition or page numbers.

Git preserves this file's bytes without line-ending normalization because
source and passage identifiers depend on its content hash. Do not reformat it.

SQLite files, journals, database backups and conversation histories are local
runtime data and are excluded by `.gitignore`. Rebuild the book database using
the ingestion, retrieval-index and memory-migration commands in the main README.
Do not upload a local database to share the application.
