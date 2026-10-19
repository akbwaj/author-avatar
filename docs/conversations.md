# Conversations and local chat

[Project overview](../README.md) · [Local chat setup](conversations.md)

Run shell commands from the repository root.

`/chat` now requires an authenticated conversation. The old
`{"question":"..."}` request is rejected. The grounded-answer provider,
retrieval, and citation validation remain the baseline. A bounded history
rewrite resolves follow-ups into a search question while the original user
message remains the question given to author generation and any separate review.
The reviewer is still standalone; it is not invoked by conversational `/chat`.

### Installation and additive database migration

Use the existing Python 3.10+ environment (3.12 recommended):

```sh
source .venv/bin/activate
python -m pip install -e '.[test]'
```

If the book database is missing, run the ingestion and FTS index commands above
first. Back up the existing database with SQLite's backup API while the app is
stopped; copying an actively written SQLite file alone is not a consistent backup:

```sh
python - <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3
source = Path('data/author_avatar.sqlite3').resolve()
backup = source.with_name('author_avatar.before-memory.sqlite3')
if not backup.exists():
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
print('Backup ready:', backup)
PY
python -m app.memory --db data/author_avatar.sqlite3
```

`app/memory/storage.py` installs conversation schema version 2 in a single
additive migration transaction (including upgrades from version 1):

| Table | Stored data |
| --- | --- |
| `conversations` | UUID, server-derived owner ID, timestamps, next message sequence. |
| `messages` | Stable message UUID, conversation ID, increasing sequence, client request ID, role, original user message or final assistant text, resolved sources and status/reason metadata. |
| `conversation_turns` | Unique conversation/request ID, message hash, worker reservation token, state, deadline/lease expiry and the completed response for duplicate replay during retention. Expired responses become content-free request tombstones. No rejected drafts are stored here. |
| `model_work` | Database-backed capacity leases shared by app workers using this database. |
| `conversation_migrations` | Applied conversation schema version. |

The migration is repeatable and preserves book tables, FTS indexes, existing
messages and conversations. It does not reset the database or take over SQLite's
`user_version`. Requests require an existing migrated database; they never
silently create a new database because of a misspelled path. Foreign keys and
unique constraints enforce message ownership relationships, sequence uniqueness,
request deduplication and at most one active reservation per conversation.
Version 2 adds indexes for message age and retained replay caches; it does not
rewrite timestamps or delete data during migration itself.

### Automatic message retention

Messages are retained for 5 days. Five days means **120 hours (432,000 seconds)**,
using server-generated UTC Unix timestamps in `messages.created_at`, not local
calendar dates. Each cleanup deletes individual messages with
`created_at < now_utc - 432000`. A message exactly at the cutoff is retained.
Reading a conversation does not change message age. Newer messages survive even
if other messages in their conversation or turn expire. Conversation records,
IDs and increasing message sequences are preserved; gaps are expected.

Run the additive migration, then optionally invoke cleanup immediately:

```sh
source .venv/bin/activate
python -m app.memory --db data/author_avatar.sqlite3
python -m app.memory --db data/author_avatar.sqlite3 --cleanup
```

Both commands are safe to repeat. Cleanup prints deletion counts, never message
content. The application also runs cleanup at startup and once per hour through
its FastAPI lifespan. Physical row deletion pauses while the application is
stopped and resumes at startup. If storage is temporarily unavailable or busy,
the app logs a content-free warning and tries again the next hour. Monitor these
warnings; an external scheduler can run the same CLI when the app is stopped.
`/health` remains a liveness probe, not a cleanup-success check.

History responses and model-context reads always filter expired messages,
independently of scheduled deletion. A conversation with no unexpired completed
turns has no prior model context. An individual unexpired orphan can appear in
history but is excluded from model context; only complete user/assistant pairs
from the same request are used. Full stored history and bounded model context
remain separate, and both obey retention.

Deleting a message removes its content, resolved citations/excerpts and status
metadata. If either message in a request expires, cleanup also clears that
request's entire cached response and message hash. Request ID, state and other
content-free reservation metadata remain as tombstones until manual conversation
deletion, so an old ID cannot execute the request again. Expired duplicate
requests return HTTP 409 `request_expired`, including before physical cleanup;
use a new ID for a new attempt. Cleanup also invalidates orphaned caches. There
are no persisted conversation summaries in this version, so there is no summary
copy to retain or invalidate; future summary storage must join this policy.

Cleanup uses short transactions with at most 200 message deletions per batch,
interleaving safely with reservations and finalization. It does not alter active
turns or capacity leases and holds no transaction during model work. A turn
records the expiry of the exact bounded history it supplies to the model. Model
call boundaries and atomic finalization reject a turn whose supplied history
expired while it was running (HTTP 409 `history_expired`). Its draft is discarded,
no final messages or response cache are written, and the active reservation is
released. There is no automatic retry. Already-running native model work cannot
be recalled, but its result cannot restore expired conversation history.

This policy covers **application storage**: message rows and response caches in
the active SQLite database. SQLite `secure_delete` is enabled for these storage
connections to clear overwritten/deleted ordinary database pages; this is not a
forensic disk-erasure guarantee. Backups, snapshots, exported histories and
external model-provider retention require separate policies. Restoring a backup
must be followed by startup or CLI cleanup before serving traffic. Book-source
passages are evidence, not conversation messages, and are unaffected. No browser
chat interface exists yet; any future interface must display
“Messages are retained for 5 days.”

### Local demo identity only

There was no authentication to reuse. **This demo authentication must be replaced
before public launch.** Two different opaque environment tokens map to fixed IDs:
`DEMO_TOKEN_A` → `demo-user-a`, `DEMO_TOKEN_B` → `demo-user-b`. Identity always comes
from `Authorization: Bearer ...`, never a request-body user ID. Missing, short,
identical or otherwise invalid server token configuration fails closed with 503;
an invalid client bearer token receives 401. Tokens are not stored in SQLite or
logged by the application. Health and public book search remain unauthenticated;
all conversation and chat routes require identity.

Generate a private local configuration file if it does not already exist:

```sh
python - <<'PY'
import os
from pathlib import Path
import secrets
path = Path('.env.demo')
if not path.exists():
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write('DEMO_TOKEN_A=' + secrets.token_urlsafe(32) + '\n')
        output.write('DEMO_TOKEN_B=' + secrets.token_urlsafe(32) + '\n')
print('Local token configuration ready; token values not displayed.')
PY
set -a
source .env.demo
set +a
```

`.env.demo` is ignored by Git; only placeholder settings appear in `.env.example`.
Do not commit token values or log authorization headers. Rotating a token does
not change its mapped owner's ID, so that user's conversation history remains.
There is no signup, password login, revocation service or account-management UI.

### Local model and application startup

The inspected machine has 48 GiB RAM. Ollama 0.24.0 is installed, and
`qwen3:4b-instruct` Q4_K_M was downloaded in the preceding milestone. It occupies
approximately **2.5 GB** and reports a native context capacity of 262,144 tokens.
Runtime memory also includes the context cache. Check availability rather than
assuming another machine already has this model:

```sh
open -a Ollama
ollama list
# Only if missing: approximately 2.5 GB download plus small metadata files.
ollama pull qwen3:4b-instruct
ollama show qwen3:4b-instruct
```

If Ollama is missing, install the macOS application first:

```sh
curl -fL https://ollama.com/download/Ollama.dmg -o /tmp/Ollama.dmg
open /tmp/Ollama.dmg
# Drag the mounted Ollama app into Applications, then open it.
```

Alternatively start `OLLAMA_NO_CLOUD=1 ollama serve` when the desktop server is
not running. The adapter uses a loopback HTTP origin and rejects cloud aliases.
Start one application worker from the repository root, in the shell with the
private demo tokens exported:

```sh
export DATABASE_PATH=./data/author_avatar.sqlite3
export LLM_PROVIDER=ollama
export OLLAMA_BASE_URL=http://localhost:11434
export OLLAMA_MODEL=qwen3:4b-instruct
export OLLAMA_NUM_CTX=16384
export MODEL_TIMEOUT_SECONDS=60
export MEMORY_CONTEXT_BYTES=4000
export CHAT_DEADLINE_SECONDS=60
export MODEL_MAX_CONCURRENT_TURNS=1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

`.env` and `.env.demo` are not automatically loaded. In each client terminal,
activate the environment and export `.env.demo` as shown above. Use the API:

```sh
CONVERSATION_ID=$(curl -fsS -X POST http://127.0.0.1:8000/conversations \
  -H "Authorization: Bearer $DEMO_TOKEN_A" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["conversation_id"])')
REQUEST_ID=$(python -c 'import uuid; print(uuid.uuid4())')
curl -i --max-time 65 http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $DEMO_TOKEN_A" \
  -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CONVERSATION_ID\",\"message\":\"What is Kriya Yoga?\",\"request_id\":\"$REQUEST_ID\"}"

# New turn, same conversation, new client request ID.
REQUEST_ID=$(python -c 'import uuid; print(uuid.uuid4())')
curl -i --max-time 65 http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $DEMO_TOKEN_A" \
  -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CONVERSATION_ID\",\"message\":\"Tell me more about it.\",\"request_id\":\"$REQUEST_ID\"}"

curl -fsS http://127.0.0.1:8000/conversations -H "Authorization: Bearer $DEMO_TOKEN_A"
curl -fsS "http://127.0.0.1:8000/conversations/$CONVERSATION_ID/messages" \
  -H "Authorization: Bearer $DEMO_TOKEN_A"
# User B receives 404 for user A's history or chat writes, not that history.
curl -i "http://127.0.0.1:8000/conversations/$CONVERSATION_ID/messages" \
  -H "Authorization: Bearer $DEMO_TOKEN_B"
# Optional owner-authorized deletion, refused while a turn is active.
# curl -i -X DELETE "http://127.0.0.1:8000/conversations/$CONVERSATION_ID" \
#   -H "Authorization: Bearer $DEMO_TOKEN_A"
```

### Request flow, memory, and grounding

1. Authentication derives the owner on the server. `POST /conversations` creates
   an empty owner-scoped conversation; `GET /conversations` lists only that
   owner's rows. Every history read, reservation, finalization, abort and delete
   checks ownership. A missing or foreign conversation returns the same 404.
2. `POST /chat` accepts exactly `conversation_id` (UUID), `message` (1–2000
   characters containing a letter or number), and client `request_id` (UUID).
   Extra fields, including user IDs, provider settings and client-supplied
   history, are rejected. The response preserves `status`, `answer`, `sources`,
   `reason`, `request_id`, and adds `conversation_id`. `X-Request-ID` echoes the
   validated client ID. Conversation creation takes no trusted body attributes.
3. A short `BEGIN IMMEDIATE` transaction reserves the turn. A partial unique
   SQLite index prevents overlapping active turns even across app processes.
   The transaction is committed and the connection closed before model work.
4. Recent unexpired completed user/assistant pairs are read only from that conversation.
   The latest whole pairs fit within `MEMORY_CONTEXT_BYTES` (default 4000,
   allowed 0–16000). If the newest pair is too large it is omitted rather than
   cut or replaced by older context. At most 50 recent pairs are considered.
   Full retained history is stored separately: the history endpoint returns the newest
   page of up to 100 messages in ascending sequence; use `limit` and
   `before_sequence` to retrieve older pages. No summarization is performed.
5. With prior turns, or reference-bearing first messages, a separate structured
   rewrite call receives the original message and bounded history. It returns
   a standalone search question or `clarification_needed`. Obvious reference-only
   first turns without history ask for clarification directly. A clarification
   returns HTTP 200 with no book sources and is stored as a completed turn.
   Invalid rewrite output is a provider error; it never falls back to guessing.
6. Existing FTS5 retrieval searches the standalone question, opening its SQLite
   read connection in a worker thread. Author generation still receives the
   **original** message, whole newly retrieved passages and bounded history in
   data-only JSON. History is explicitly untrusted conversational context,
   never evidence. Old citations do not become valid current citations merely
   by appearing in history. The separate reviewer can still receive the saved
   author request, original question, and exact supplied passages.
7. The author schema and existing citation/quotation validator run unchanged.
   IDs must come from actual supplied passages; markers and ID lists must agree;
   supported double-quoted quotations must match cited stored text. The server
   prefixes the AI identity disclosure and resolves source, edition, chapter,
   title and excerpt metadata from canonical retrieval records. No page numbers
   are invented. Empty retrieval/context or model abstention stays explicit.
8. A second short write transaction rechecks ownership, reservation token,
   deadline, original-message hash and supplied-history expiry. It atomically appends the original user
   message and final user-facing assistant response at stable increasing
   sequences, records the replayable response, and releases model capacity.
   Failed calls, invalid drafts and rejected outputs append neither message.
   Review findings and intermediate drafts are never conversation history.

History/reference resolution and semantic grounding still depend on model
behavior. Schema validation cannot prove that a rewrite preserved intent, that
an ambiguous reference was recognized, or that every claim follows from a cited
passage. The prompt separation is not proof of injection resistance. The
standalone reviewer is not part of `/chat`, and no reviewer accuracy evaluation
is claimed. Book accounts must be attributed to the book, not presented as
externally verified facts.

### Idempotency, failures, deadlines and capacity

The request ID is unique within a conversation. Retrying an unexpired completed ID with
identical message text returns the exact cached response and writes no new
messages. Changing the message under an existing ID returns 409
`request_conflict`. An active duplicate returns 409 `request_in_progress`;
a different overlapping request returns 409 `conversation_busy`. A failed or
expired ID returns 409 `request_failed`: retry after that known failure with a
new request ID. After an uncertain network failure, first retry the same ID
so a committed answer is not duplicated.
After message retention expires, that ID instead returns 409 `request_expired`
and never starts another generation. Tombstones survive scheduled cleanup.

Reservations have unpredictable worker tokens. A late worker cannot finalize
an expired/superseded reservation. Normal failure or async cancellation marks
the request failed and releases capacity in cleanup. A crashed worker or failed
cleanup is recovered lazily on a later request after the lease expires (the
turn deadline plus five seconds). Stale work cannot append messages or release
a newer worker's lease. No model call holds an open SQLite transaction.

`CHAT_DEADLINE_SECONDS` defaults to 60 (allowed 0.01–120) and applies to the
reserved turn's history loading, rewrite, retrieval, author work and final
commit. One outer async timeout shares the remaining budget across stages;
finalization also checks the persisted deadline. `MODEL_TIMEOUT_SECONDS`
remains a per-call limit (0.01–60). Expiry returns HTTP 504 `provider_timeout`,
not an abstention or a partial answer. Python cancellation is cooperative and
SQLite statements/cleanup can take a short bounded wait; this is not a hard
real-time deadline or proof the native model server immediately stopped work.

The enforced current model-call budget is **at most two generations per turn**:
one optional rewrite plus one author call. Clarification may stop after the
rewrite; empty first-turn retrieval needs no generation. The wrapper counts
calls and rejects a third call; there are no automatic retries. The standalone
reviewer remains outside this workflow. If draft/review/one revision/final
review are later integrated, their previous planned four-call ceiling must
become **five including rewriting** and share the same overall deadline.
No such revision workflow is claimed as implemented.

A database-backed `model_work` lease limits active model-producing turns to
`MODEL_MAX_CONCURRENT_TURNS` (default 1; allowed 1–4). It is shared by app
workers pointing at the same DB. When capacity is occupied, another conversation
gets HTTP 503 `model_busy`, and its reservation is released. Configure all app
workers consistently; different databases, standalone scripts and other Ollama
clients do not share this limit. Ollama's own server concurrency/queue limits
remain relevant if cancelled native work continues.

Other errors preserve safe messages: 502 `invalid_model_output` or
`provider_failure`; 503 `provider_configuration_error`, `model_unavailable`,
`provider_unavailable`, `retrieval_unavailable` or
`conversation_storage_unavailable`. Upstream bodies, credentials, prompts and
book/history content are not logged by application code. Conversation endpoint
errors use FastAPI's `detail` envelope; `/chat` errors retain `error` and
`request_id`. Missing authentication is handled before the chat operation.

### Provider and context compatibility

`LLM_PROVIDER` selects `fake` or `ollama`; unset falls back to the legacy
`MODEL_PROVIDER` and then `fake`. Fake defaults abstain without network calls;
scripted fake responses exercise rewriting and answered behavior in tests.
Ollama uses the existing async `httpx` adapter: `/api/show`, then one `/api/chat`
per generation, `stream: false`, temperature 0, schema in `format`, and at most
1536 output tokens. The transport supports the recommended byte-level Llama
and Qwen families and rejects incompatible/remote models.

`OLLAMA_NUM_CTX` accepts 8192–32768, default 16384. Whole evidence is selected
under both the 16,000-character data cap and a conservative UTF-8 byte budget
including instructions, schema, original question and history. It reserves
1536 output tokens and 2048 template tokens; the default full prompt cap is
12,800 bytes. History can therefore reduce the number of whole passages that
fit. Oversized passages are skipped intact; no identifiers are truncated.
The full response envelope is bounded to 128,000 bytes and generation JSON to
24,000 characters. Application validation remains authoritative.

Ollama 0.24.0 crashes compiling the application's large `maxLength: 6000`
grammar constraint. The existing adapter omits `maxLength` from its transport
schema only; the full schema remains in the trusted prompt and application
validation enforces all string lengths. Earlier Llama 3.2 3B smoke responses
failed citation consistency. The preceding Qwen smoke returned a validated
chapter-26 answer; that observation is not a memory or reviewer evaluation.
The separate live `python -m scripts.smoke_ollama` remains a direct grounded
pipeline probe, not an authenticated conversation API test.

Official implementation references:
[Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[chat API](https://docs.ollama.com/api/chat),
[model metadata](https://docs.ollama.com/api-reference/show-model-details),
[macOS installation](https://docs.ollama.com/macos),
[Qwen3 4B Instruct](https://ollama.com/library/qwen3:4b-instruct),
[SQLite transactions](https://www.sqlite.org/lang_transaction.html), and
[Python constant-time comparison](https://docs.python.org/3/library/hmac.html#hmac.compare_digest).

### Tests and deployment limits

```sh
python -m pytest tests/test_retention.py tests/test_memory.py tests/test_chat.py tests/test_ollama.py
python -m pytest
```

The memory suite uses fake providers and synthetic book text. It checks cross-user
read/write/delete isolation, isolated conversations, preceding history and original
question preservation, bounded whole turns, duplicate replay/conflicts, busy
responses, independent SQLite reservation races, capacity leases, stale fencing,
cancellation/failure cleanup, malformed rewrites, shared deadlines and rejection
of historical IDs absent from current evidence. Baseline author HTTP tests now
create a fresh authenticated conversation for each generation probe. No live
model memory accuracy or evaluation result is claimed.

The retention suite uses a controllable UTC clock and fake model responses. It
checks the strict 120-hour boundary, mixed-age individual deletion, API/context
filtering before cleanup, orphan handling, citation/cache deletion and permanent
request deduplication, repeatable batched cleanup, active leases, expiry during
rewrite/author calls, ownership, startup, hourly scheduling and the cleanup CLI.
It makes no live model or external provider calls.

This remains a localhost demo. Replace static-token authentication before public
launch. SQLite and token files are plaintext and rely on host filesystem access
controls; backups contain conversation data and need their own retention policy.
There is no history encryption, administrator UI or distributed scheduler. Use one application
worker and one shared local database for the documented setup; multiple workers
must use the same database and identical deadline/capacity configuration. Database
leases handle crashes but cannot cancel work in another process or an external
model client. The existing reviewer is still standalone. No frontend, embeddings,
summary memory, evaluation dataset or automatic revision loop is added here.
