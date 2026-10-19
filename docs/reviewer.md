# Evidence reviewer

[Project overview](../README.md) · [Local chat setup](conversations.md)

Run shell commands from the repository root.

`app/generation/reviewer.py` adds a separate evidence-review prompt and one
async `ModelProvider.generate()` call. It uses the existing provider factory
and configuration, including the installed local `qwen3:4b-instruct` model.
The reviewer itself adds no dependency, model download, endpoint or revision loop.
It remains separate from conversational `/chat`; review findings are not added to `ChatResponse`.

`review_draft(question, draft_answer, author_request, *, provider=None,
settings=None)` accepts the original question, the draft text, and the saved
`GenerationRequest` actually sent to the author. It reads that request's
data-only JSON, verifies the question matches, and forwards every supplied
passage unchanged with its full text, ID, source/edition/section IDs, chapter
number and title, and existing citation marker. It does not re-run retrieval,
use display excerpts, or select a smaller evidence subset. The caller must
retain the original author request; the public `/chat` response is not a
substitute for this snapshot.

The separate reviewer prompt asks for unsupported claims, citations that do
not support their accompanying claims, missing or invented citations,
inexact quotations (including single-quoted text), contradictions, invented
personal memories/author identity, failure to answer the original question,
and book accounts presented as externally verified facts. It explicitly
forbids background knowledge and treats the draft, question and all passage
fields as untrusted data. It requests only concise, actionable findings,
never hidden chain-of-thought or a replacement answer.

`app/generation/review_models.py` defines the strict internal response schema:

```json
{
  "verdict": "revise",
  "issues": [
    {
      "claim": "Mira found a golden spaceship.",
      "category": "unsupported_claim",
      "passage_ids": ["passage-amber"],
      "explanation": "Replace golden spaceship with the amber telescope described in the passage."
    }
  ]
}
```

This example is fictional test data, not book text or a live review result.
Categories are `unsupported_claim`, `citation_support`, `quotation`,
`contradiction`, `invented_memory`, `question_relevance`, and `external_fact`.
Every reported issue is blocking. `pass` requires `issues: []`; both `revise`
and `insufficient_evidence` require at least one finding. The prompt requires
`revise` when the supplied evidence can support a useful corrected answer,
and `insufficient_evidence` otherwise. Determining that distinction is a
model judgment, not a guarantee provided by schema validation.

The schema rejects unknown verdicts/categories/fields, blank findings,
duplicate or non-string passage IDs, and more than eight issues. Claims are
limited to 1000 characters and explanations to 500. Each issue references at
most five distinct IDs, all of which must occur in the actual author snapshot;
the request schema also constrains IDs to that set. An empty ID list is valid
when no passage is relevant. Invented draft citations can be described in the
finding text but cannot appear as valid reviewer references. Extra fields
such as `chain_of_thought` or a replacement `answer` are rejected.

The draft is limited to 8000 characters, allowing for the existing AI
disclosure. The entire reviewer prompt, schema, original question, full draft
and all original passages count toward the configured provider budget.
Review adds input beyond author generation, so an author request that fits
16K context might need a larger context for review. The installed Qwen model
supports a larger context; pass review settings with `context_tokens=32768`
when needed. If the complete input still does not fit, a safe
`ProviderConfigurationError` is raised before any model call. Evidence and
draft text are never silently truncated to make review fit.

For a future internal coordinator, the call after author generation would be:

```python
from dataclasses import replace
from app.generation.reviewer import review_draft

# Keep question, draft, author_request and author_settings from author generation.
# Same provider/model, with additional room for the draft and review schema.
review_settings = replace(author_settings, context_tokens=32768)
findings = await review_draft(
    question, draft.answer, author_request, settings=review_settings,
)
# findings stays internal; no coordinator or HTTP integration is added here.
```

The reviewer uses the same bounded `MODEL_TIMEOUT_SECONDS`, raw output limit,
Ollama structured-output transport and safe provider errors as the baseline.
Timeout cancels the call; malformed review is an `InvalidModelOutput`, never
an implicit `pass` or abstention. There are no automatic retries, repairs or
revisions. The default author's fake response is not a review verdict and is
rejected; offline reviewer tests supply scripted `FakeProvider` responses.
The implementation adds no prompt, draft, passage or findings logging.

Install and test from the repository root using the existing environment:

```sh
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest tests/test_reviewer.py
python -m pytest
```

The focused suite verifies parsing, verdict/issue consistency, every category,
unknown/unsupplied passage IDs, exact snapshot preservation, untrusted-data
separation, full-input context rejection, timeout/cancellation, sanitized
failures, two separate author/reviewer calls, and the existing Ollama adapter
with mocked HTTP. All tests are offline. They verify contracts and boundaries,
not semantic reviewer accuracy or prompt-injection resistance. No live reviewer
test or evaluation accuracy is claimed. Use the authenticated conversation setup above for `/chat`; reviewer calls remain internal and explicit.
