"""The default test suite is offline regardless of the caller's shell settings."""

import pytest


@pytest.fixture(autouse=True)
def offline_model_environment(monkeypatch, tmp_path):
    # Lifespan maintenance must never touch a developer's configured database.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "unconfigured.sqlite3"))
    monkeypatch.setenv("DEMO_TOKEN_A", "test-only-user-a-opaque-placeholder-00000000")
    monkeypatch.setenv("DEMO_TOKEN_B", "test-only-user-b-opaque-placeholder-00000000")
    for name in ("MEMORY_CONTEXT_BYTES", "CHAT_DEADLINE_SECONDS", "MODEL_MAX_CONCURRENT_TURNS"):
        monkeypatch.delenv(name, raising=False)
    for name in ("LLM_PROVIDER", "OLLAMA_MODEL", "OLLAMA_BASE_URL", "OLLAMA_NUM_CTX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("MODEL_NAME", "offline-fake")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "10")
