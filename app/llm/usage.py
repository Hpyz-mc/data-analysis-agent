from pydantic import BaseModel, Field
from typing import Literal


class ModelCallUsage(BaseModel):
    model: str
    attempt: int
    status: str = "pending"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    retry_reason: str | None = None
    estimated_prompt_tokens: int | None = None
    output_token_limit: int | None = None
    finish_reason: str | None = None
    phase: Literal["planning", "decision", "report"] | None = None


class AnalysisUsage(BaseModel):
    calls: list[ModelCallUsage] = Field(default_factory=list)
    model_calls: int = 0
    retries: int = 0
    unknown_calls: int = 0
    complete: bool = True
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    total_tokens: int | None = 0
    cache_hit_tokens: int | None = 0
    budget_stop_reason: str | None = None

    @classmethod
    def aggregate(cls, calls: list[ModelCallUsage]):
        unknown = sum(c.total_tokens is None for c in calls)
        totals = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "cache_hit_tokens"):
            values = [getattr(c, field) for c in calls]
            totals[field] = None if any(v is None for v in values) else sum(values)
        return cls(calls=calls, model_calls=len(calls),
                   retries=sum(c.attempt > 1 for c in calls), unknown_calls=unknown,
                   complete=all(c.prompt_tokens is not None and c.completion_tokens is not None
                                and c.total_tokens is not None for c in calls), **totals)
