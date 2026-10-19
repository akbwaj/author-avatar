"""Local Ollama transport; never used by ingestion or retrieval."""

import asyncio
import json

import httpx

from .provider import (
    GenerationRequest, InvalidModelOutput, ModelSettings, ModelUnavailable,
    ProviderConfigurationError, ProviderError, ProviderTimeout, ProviderUnavailable,
    MAX_GENERATED_TOKENS, TEMPLATE_RESERVE,
)

MAX_RESPONSE_BYTES = 128_000


def grammar_schema(value):
    """Keep the schema shape without large string repetition bounds.

    Ollama 0.24.0's llama.cpp grammar compiler rejects maxLength=6000 and
    crashes its runner. The trusted prompt still contains the full schema;
    the unchanged application schema enforces every length after generation.
    """
    if isinstance(value, dict):
        return {key: grammar_schema(item) for key, item in value.items() if key != "maxLength"}
    if isinstance(value, list):
        return [grammar_schema(item) for item in value]
    return value


class OllamaProvider:
    def __init__(self, settings: ModelSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def _post(self, client: httpx.AsyncClient, path: str, payload: dict) -> dict:
        # Streaming the HTTP body bounds memory even though Ollama generation is
        # non-streaming JSON. Never echo an upstream body, prompt or exception.
        async with client.stream("POST", path, json=payload) as response:
            if response.status_code == 404:
                raise ModelUnavailable("Configured local model was not found.")
            if response.status_code == 503:
                raise ProviderUnavailable("Local generation service is unavailable.")
            if not response.is_success:
                raise ProviderError("Local generation request failed.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise InvalidModelOutput("Local response exceeded the size limit.")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise InvalidModelOutput("Local response was not JSON.") from None
        if not isinstance(data, dict) or "error" in data:
            raise InvalidModelOutput("Invalid local response envelope.")
        return data

    async def generate(self, request: GenerationRequest) -> str:
        try:
            # A total deadline includes model discovery, loading and generation;
            # per-operation HTTP timeouts alone would not bound the whole call.
            return await asyncio.wait_for(self._generate(request), self.settings.timeout_seconds)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise ProviderTimeout("Local generation timed out.") from None
        except httpx.RequestError:
            raise ProviderUnavailable("Cannot connect to the local generation service.") from None

    async def _generate(self, request: GenerationRequest) -> str:
        async with httpx.AsyncClient(
            base_url=self.settings.base_url, timeout=self.settings.timeout_seconds,
            transport=self.transport, trust_env=False, follow_redirects=False,
        ) as client:
            details = await self._post(client, "/api/show", {"model": self.settings.model})
            # Reject cloud-backed aliases, including aliases without ':cloud'.
            if details.get("remote_model") or details.get("remote_host"):
                raise ProviderConfigurationError("Only local models are supported.")
            info = details.get("model_info")
            architecture = info.get("general.architecture") if isinstance(info, dict) else None
            capacity = info.get(f"{architecture}.context_length") if isinstance(info, dict) else None
            capabilities = details.get("capabilities")
            # The byte budget is intended for the recommended byte-level BPE
            # families. Unknown tokenizers must not silently use that assumption.
            if architecture not in {"llama", "qwen2", "qwen3"}:
                raise ProviderConfigurationError("Unsupported local model tokenizer family.")
            if (type(capacity) is not int or capacity < self.settings.context_tokens
                    or not isinstance(capabilities, list) or "completion" not in capabilities):
                raise ProviderConfigurationError("Local model does not support the configured context or chat.")
            template = details.get("template", "")
            system = details.get("system", "")
            if (not isinstance(template, str) or not isinstance(system, str)
                    or len((template + system).encode("utf-8")) > TEMPLATE_RESERVE):
                raise ProviderConfigurationError("Local model template exceeds the reserved budget.")
            budget = self.settings.max_prompt_bytes
            if budget is None or request.prompt_bytes > budget:
                raise ProviderConfigurationError("Local prompt exceeds its context budget.")
            data = await self._post(client, "/api/chat", {
                "model": self.settings.model,
                "messages": [
                    {"role": "system", "content": request.system_with_schema},
                    {"role": "user", "content": request.evidence_json},
                ],
                "format": grammar_schema(request.output_schema),
                "stream": False,
                "options": {"temperature": 0, "num_ctx": self.settings.context_tokens,
                            "num_predict": MAX_GENERATED_TOKENS},
            })
        message = data.get("message")
        if (data.get("done") is not True or data.get("done_reason") != "stop"
                or not isinstance(message, dict) or message.get("role") != "assistant"
                or not isinstance(message.get("content"), str) or not message["content"].strip()
                or message.get("tool_calls")):
            raise InvalidModelOutput("Local generation was incomplete or malformed.")
        # Application schema and canonical citations are checked by the existing
        # coordinator. There is no repair request or automatic generation retry.
        return message["content"]
