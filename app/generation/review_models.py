"""Strict internal findings from evidence review, never a public chat answer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IssueCategory = Literal[
    "unsupported_claim", "citation_support", "quotation", "contradiction",
    "invented_memory", "question_relevance", "external_fact",
]
MAX_REVIEW_ISSUES = 8


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    claim: str = Field(min_length=1, max_length=1000)
    category: IssueCategory
    passage_ids: list[str] = Field(max_length=5)
    explanation: str = Field(min_length=1, max_length=500)

    @field_validator("claim", "explanation")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("finding text must not be blank")
        return value

    @field_validator("passage_ids")
    @classmethod
    def distinct_ids(cls, value: list[str]) -> list[str]:
        if any(not pid.strip() for pid in value) or len(value) != len(set(value)):
            raise ValueError("passage IDs must be nonblank and unique")
        return value


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["pass", "revise", "insufficient_evidence"]
    issues: list[ReviewIssue] = Field(max_length=MAX_REVIEW_ISSUES)

    @model_validator(mode="after")
    def verdict_matches_findings(self) -> "ReviewResult":
        # Every reported issue is blocking. There are no advisory findings.
        if (self.verdict == "pass") != (not self.issues):
            raise ValueError("pass requires no issues; other verdicts require findings")
        return self
