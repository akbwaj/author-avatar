# Author Avatar

An AI interpretation of Paramahansa Yogananda, grounded in *Autobiography of a
Yogi*. The persona identifies itself as AI and answers from retrieved book
passages with validated chapter citations.

Python 3.10+ · FastAPI · SQLite FTS5 · Ollama

## Current status

- Offline book ingestion and keyword retrieval.
- Grounded answers through Ollama, with an offline fake provider for tests.
- Citation and quotation validation against supplied passages.
- Evidence review with at most one revision before returning an answer.
- Owner-isolated conversations with duplicate-request protection.
- Messages retained for 5 days, with automatic cleanup.

A browser chat interface is planned.

## Quickstart

From the repository root, using Python 3.12 or another supported version:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'

python -m app.ingestion \
  --source 'data/raw/Autobiography of a YOGI _ Project Gutenberg.html' \
  --db data/author_avatar.sqlite3 \
  --report reports/ingestion-inspection.md
python -m app.retrieval index --db data/author_avatar.sqlite3
python -m app.memory --db data/author_avatar.sqlite3

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open [API documentation](http://127.0.0.1:8000/docs) or check
[health](http://127.0.0.1:8000/health).

The default fake provider runs offline and abstains. For authenticated chat and
local model installation, follow the [local chat guide](docs/conversations.md).
Configuration is documented in [.env.example](.env.example); environment files
are not loaded automatically. Demo authentication must be replaced before
public deployment.

## Tests

```sh
python -m pytest
```

Tests use local book fixtures, fake providers and mocked HTTP responses. They
require no AWS credentials, running Ollama server or model download.

## Development roadmap

Next: add the browser chat interface and build an evaluation dataset. See the
[architecture and roadmap](docs/architecture.md) for the planned layers.

## Documentation

- [Installation and optional AWS probe](docs/setup.md)
- [Book ingestion](docs/ingestion.md) and [keyword retrieval](docs/retrieval.md)
- [Local chat, authentication and conversation memory](docs/conversations.md)
- [Message retention](docs/conversations.md#automatic-message-retention)
- [Evidence review and revision workflow](docs/reviewer.md)
- [GitHub upload and CI](docs/github.md)
- [Book provenance and notices](data/README.md)

The application code has no selected license. The bundled book retains its
original Project Gutenberg notices and license.
