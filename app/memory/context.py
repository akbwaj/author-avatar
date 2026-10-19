"""Bound whole completed turns; never summarize or treat history as evidence."""
from dataclasses import dataclass
import json
import math
import os
import re

from app.generation.provider import ProviderConfigurationError


@dataclass(frozen=True)
class MemorySettings:
    history_bytes: int = 4000
    deadline_seconds: float = 60
    concurrent_turns: int = 1

    @classmethod
    def from_environment(cls):
        try:
            value = cls(int(os.getenv("MEMORY_CONTEXT_BYTES", "4000")),
                        float(os.getenv("CHAT_DEADLINE_SECONDS", "60")),
                        int(os.getenv("MODEL_MAX_CONCURRENT_TURNS", "1")))
        except ValueError:
            raise ProviderConfigurationError("Invalid conversation configuration.") from None
        if (not 0 <= value.history_bytes <= 16000 or not math.isfinite(value.deadline_seconds)
                or not 0.01 <= value.deadline_seconds <= 120 or not 1 <= value.concurrent_turns <= 4):
            raise ProviderConfigurationError("Invalid conversation context, deadline or concurrency configuration.")
        return value


def completed_pairs(rows: list[dict]):
    # Individual retention/pagination can leave an orphan. Never combine messages
    # from different requests into a turn, or use an orphan as model context.
    for index in range(len(rows) - 1, 0, -1):
        pair = rows[index - 1:index + 1]
        if ([row["role"] for row in pair] == ["user", "assistant"]
                and pair[0]["request_id"] == pair[1]["request_id"]
                and pair[1]["sequence"] == pair[0]["sequence"] + 1):
            yield pair


def bounded_history(rows: list[dict], byte_budget: int) -> list[dict]:
    selected = []
    for pair in completed_pairs(rows):
        candidate = [{"role": row["role"], "content": row["content"], "sequence": row["sequence"]}
                     for row in pair] + selected
        if len(json.dumps(candidate, ensure_ascii=False).encode()) > byte_budget:
            break  # Never skip the newest turn and imply older context is current.
        selected = candidate
    return selected


def needs_missing_context(message: str) -> bool:
    # Clear reference-only first turns cannot be resolved from nonexistent memory.
    return bool(re.fullmatch(
        r"\s*(tell me more( about (him|her|it|them|that|this))?|continue|go on|why|why is that|what about (him|her|it|them|that|this))\s*[?.!]*\s*",
        message, flags=re.IGNORECASE))


def has_reference(message: str) -> bool:
    return bool(re.search(r"\b(he|she|him|her|his|hers|they|them|their|it|its|this|that|these|those)\b",
                          message, flags=re.IGNORECASE))
