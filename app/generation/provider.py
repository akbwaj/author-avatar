"""Replaceable asynchronous provider boundary. No credentials or passage logging."""

from dataclasses import dataclass
import json
import math
import os
from typing import Protocol
from urllib.parse import urlsplit

from .models import GenerationResult

MAX_GENERATED_TOKENS = 1536
TEMPLATE_RESERVE = 2048


class ProviderError(RuntimeError):
    """Provider execution failed, rather than finding insufficient evidence."""


class ProviderConfigurationError(ProviderError):
    """Invalid or unsupported server-side provider configuration."""


class ProviderTimeout(ProviderError):
    """Generation exceeded the configured deadline."""


class ProviderUnavailable(ProviderError):
    """The configured generation service cannot be reached."""


class ModelUnavailable(ProviderUnavailable):
    """The selected model is not installed or cannot be loaded."""


class ProviderBusy(ProviderUnavailable):
    """The configured model concurrency capacity is occupied."""


class InvalidModelOutput(ProviderError):
    """The provider response failed structural or evidence integrity checks."""


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    evidence_json: str
    output_schema: dict

    @property
    def system_with_schema(self) -> str:
        return self.system + "\nOutput JSON schema:\n" + json.dumps(self.output_schema, ensure_ascii=False)

    @property
    def prompt_bytes(self) -> int:
        return len((self.system_with_schema + self.evidence_json).encode("utf-8"))


class ModelProvider(Protocol):
    async def generate(self, request: GenerationRequest) -> str:
        """Return JSON matching output_schema; implementations must not block the loop."""


class FakeProvider:
    """Offline default abstains; tests may supply one scripted JSON response.

    No calls or passages are retained, so the server remains stateless. A test
    subclass can capture inputs locally when it needs to inspect the contract.
    """

    def __init__(self, response: str | None = None, *, model: str = "offline-fake"):
        self.model = model
        self.response = response if response is not None else json.dumps({
            "status": "insufficient_evidence",
            "answer": "I cannot provide a grounded answer with the offline fake provider.",
            "citations": [],
            "reason": "The offline fake does not infer answers from evidence. Select a configured live provider to generate answers.",
        })

    async def generate(self, request: GenerationRequest) -> str:
        return self.response


@dataclass(frozen=True)
class ModelSettings:
    provider: str = "fake"
    model: str = "offline-fake"
    timeout_seconds: float = 10.0
    base_url: str = "http://localhost:11434"
    context_tokens: int = 16_384

    @property
    def max_prompt_bytes(self) -> int | None:
        # Conservative byte-per-token bound for the recommended byte-level
        # Llama tokenizer, rather than assuming four English characters/token.
        # Reserve output tokens and template/system overhead outside our prompt.
        return (self.context_tokens - MAX_GENERATED_TOKENS - TEMPLATE_RESERVE
                if self.provider == "ollama" else None)

    @classmethod
    def from_environment(cls) -> "ModelSettings":
        provider = os.getenv("LLM_PROVIDER", os.getenv("MODEL_PROVIDER", "fake"))
        try:
            timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60" if provider == "ollama" else "10"))
        except ValueError:
            raise ProviderConfigurationError("Invalid model timeout configuration.") from None
        if not math.isfinite(timeout) or not 0.01 <= timeout <= 60:
            raise ProviderConfigurationError("Model timeout must be between 0.01 and 60 seconds.")
        if provider == "fake":
            model = os.getenv("MODEL_NAME", "offline-fake")
            if model != "offline-fake":
                raise ProviderConfigurationError("Unsupported fake model.")
            return cls(provider=provider, model=model, timeout_seconds=timeout)
        if provider != "ollama":
            raise ProviderConfigurationError("Unsupported model provider.")
        model = os.getenv("OLLAMA_MODEL", "").strip()
        if not model or model.endswith(":cloud"):
            raise ProviderConfigurationError("Set OLLAMA_MODEL to an installed local model.")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        try:
            url = urlsplit(base_url)
            valid_port = url.port is None or 1 <= url.port <= 65535
        except ValueError:
            raise ProviderConfigurationError("Invalid local model URL.") from None
        if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username or url.password or url.path or url.query or url.fragment or not valid_port):
            raise ProviderConfigurationError("OLLAMA_BASE_URL must be a local HTTP origin.")
        try:
            context = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
        except ValueError:
            raise ProviderConfigurationError("Invalid local context size.") from None
        if not 8192 <= context <= 32768:
            raise ProviderConfigurationError("OLLAMA_NUM_CTX must be between 8192 and 32768.")
        return cls(provider=provider, model=model, timeout_seconds=timeout,
                   base_url=base_url, context_tokens=context)


def create_provider(settings: ModelSettings) -> ModelProvider:
    if settings.provider == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(settings)
    if settings.provider != "fake" or settings.model != "offline-fake":
        raise ProviderConfigurationError("Unsupported model configuration.")
    # The fake never reads credentials or calls a service.
    return FakeProvider(model=settings.model)


def output_schema() -> dict:
    return GenerationResult.model_json_schema()
