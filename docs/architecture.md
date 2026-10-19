# Architecture and roadmap

[Project overview](../README.md) · [Local chat setup](conversations.md)

Run shell commands from the repository root.

Keep these responsibilities separate as they are added:

| Layer | Responsibility |
| --- | --- |
| Ingestion | Load the selected edition; store passages and source metadata in SQLite. Preserve edition, source, chapter, and passage identifiers. |
| Retrieval | Return relevant passages and their identifiers for chapter-level citations. |
| Generation | Author-persona and evidence-review agents, using a replaceable provider interface and an offline fake. |
| Orchestration | Deterministic sequence: retrieve → draft → review → optionally revise once → final validation. Reject or qualify unsupported claims rather than looping. |
| HTTP | Validate requests and expose application results through FastAPI; `app/main.py` is the current entry point. |
| Browser | Simple HTML/CSS/JavaScript text chat, displaying AI identity and chapter citations. |

Book text is untrusted evidence, never instructions. Citations must resolve to
stored source records; do not invent page numbers. Quotations must match source
text. Credentials stay on the server and outside version control. SQLite uses
Python's standard-library `sqlite3`; no database dependency is needed yet.

## Development roadmap

1. **Foundation and ingestion.** Complete this scaffold and health test,
   then select and record an edition/source and ingest chapter-labelled passages
   into SQLite with stable identifiers. Verify metadata and ingestion integrity.
2. **Retrieval and citations.** Add retrieval independently of generation;
   verify relevant results and chapter citations against the stored text.
3. **Generation and coordination.** Add a provider interface and offline
   fake, persona agent, evidence reviewer, and deterministic coordinator with at
   most one revision. Test unsupported claims and instruction-like book text.
4. **Browser chat and evaluation.** Connect the simple interface, display
   AI identity and citations, and run an explicit question set. Report only
   observed results, limitations, and failures.

The current implementation includes generation, citation integrity, and a
standalone reviewer. `/chat` makes at most one history rewrite and one author-generation attempt, with
no review, revision or retry loop. Reviewer integration and an evaluation
dataset remain deferred.
