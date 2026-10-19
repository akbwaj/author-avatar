# Installation and basic usage

[Project overview](../README.md) · [Local chat setup](conversations.md)

Run shell commands from the repository root.

Use Python 3.10 or newer. From the repository root, on macOS/Linux:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
cp .env.example .env
```

If using another supported Python version, replace `python3.12` with that
interpreter. The existing health endpoint needs no credentials or environment
variables. `.env` is not auto-loaded. Retrieval reads `DATABASE_PATH` from the
process environment; chat also reads the model settings described below.

The optional standalone AWS probe has separate dependencies:

```sh
python -m pip install -e '.[bedrock]'
# Set AWS_REGION, BEDROCK_MODEL_ID and AWS_BEARER_TOKEN_BEDROCK in ignored .env.
python scripts/test_bedrock.py
```

This is a live, separately invoked probe and may incur provider charges. It is
not the configured `/chat` provider and is not run by offline tests or CI.

## Run

```sh
source .venv/bin/activate
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
curl http://127.0.0.1:8000/health
```

Expected response: `{"status":"ok"}`. API documentation is at
http://127.0.0.1:8000/docs.

## Test

```sh
source .venv/bin/activate
python -m pytest
```

The health test exercises the actual ASGI application through FastAPI's `TestClient`
and asserts HTTP status, JSON content type, and the exact response body. It runs
offline without a model provider, book data, or running server. Ingestion tests
also run offline, using the actual HTML in `data/raw` and small synthetic fixtures.
