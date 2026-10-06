"""Local conservative admission limits, not provider billing estimates."""
from dataclasses import dataclass
from time import monotonic

import httpx


@dataclass(frozen=True)
class LLMLimits:
    timeout_seconds: float = 60
    request_deadline_seconds: float = 180
    network_retries: int = 0
    max_calls: int = 6
    max_output_tokens: int = 1800
    prompt_token_budget: int = 24000
    request_token_budget: int = 50000

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, "llm_" + name, field.default)
                      for name, field in cls.__dataclass_fields__.items()})

    def timeout(self, started):
        remaining = self.request_deadline_seconds - (monotonic() - started)
        seconds = max(0.1, min(remaining, self.timeout_seconds))
        return httpx.Timeout(seconds, connect=min(5, seconds), write=min(15, seconds), pool=min(5, seconds))


def estimated_prompt_tokens(messages):
    # UTF-8 byte count is a conservative admission proxy for byte-tokenizers.
    # It is deliberately not exposed as measured/billed provider tokens.
    return sum(len(message["content"].encode("utf-8")) + 64 for message in messages)


def budget_stop_code(message):
    for text, code in (("等待时限", "deadline"), ("调用次数", "model_call_budget"),
                       ("上下文", "prompt_budget"), ("Token", "request_token_budget")):
        if text in message:
            return code
    return "budget"


def budget_error(limits, calls, messages, started):
    estimated = estimated_prompt_tokens(messages)
    if monotonic() - started >= limits.request_deadline_seconds:
        return "本轮模型调用已达到等待时限，请缩小分析范围后重试。"
    if len(calls) >= limits.max_calls:
        return "本轮已达到模型调用次数预算，停止继续调用，请缩小分析范围后重试。"
    if estimated > limits.prompt_token_budget:
        return "本次上下文超过输入预算，未发送给模型；请明确数据表并缩小查询范围。"
    reserved = sum(call.total_tokens if call.total_tokens is not None else
                   (call.estimated_prompt_tokens or 0) + (call.output_token_limit or 0) for call in calls)
    if reserved + estimated + limits.max_output_tokens > limits.request_token_budget:
        return "本轮已达到 Token 准入预算，停止继续调用，请缩小分析范围后重试。"
    return None
