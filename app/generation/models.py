"""Strict output contract shared by providers and citation validation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AI_IDENTITY = (
    "This is an AI interpretation of Paramahansa Yogananda based on "
    "Autobiography of a Yogi."
)
MAX_OUTPUT_CHARACTERS = 24_000


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["answered", "insufficient_evidence"]
    answer: str = Field(min_length=1, max_length=6000)
    citations: list[str] = Field(max_length=5)
    reason: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_status(self) -> "GenerationResult":
        if not self.answer.strip():
            raise ValueError("answer must not be blank")
        if len(self.citations) != len(set(self.citations)):
            raise ValueError("citations must be unique")
        if self.status == "answered":
            if not self.citations or self.reason is not None:
                raise ValueError("answered requires citations and no abstention reason")
        elif self.citations or not self.reason or not self.reason.strip():
            raise ValueError("abstention requires a reason and no citations")
        return self
