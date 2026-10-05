import logging
from time import monotonic, sleep

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, ValidationError, model_validator
from typing import Literal, Self

from app.agent.prompts import PLANNING_PROMPT, REPORT_PROMPT, SYSTEM_PROMPT, build_user_prompt, uses_scope_planning
from app.agent.state import AgentState, AnalysisPlan
from app.core.config import get_settings
from app.tools.python_tool import PythonAnalysisRequest
from app.tools.chart_tool import ChartRequest
from app.llm.usage import AnalysisUsage, ModelCallUsage
from app.llm.policy import LLMLimits, budget_error, budget_stop_code, estimated_prompt_tokens


class LLMError(RuntimeError):
    """Raised when the language model cannot provide a usable decision."""

    def __init__(self, message, *, code="model_error"):
        super().__init__(message)
        self.code = code


logger = logging.getLogger("uvicorn.error")


class AgentDecision(BaseModel):
    action: Literal["sql", "python", "chart", "finish"]
    reasoning: str
    plan: AnalysisPlan | None = None
    sql: str | None = None
    python: PythonAnalysisRequest | None = None
    chart: ChartRequest | None = None
    content: str

    @model_validator(mode="after")
    def require_sql_for_query(self) -> Self:
        if self.plan is not None and self.plan.analysis_context is not None:
            return self  # The graph validates the scope and generates its own tool inputs.
        if self.action == "sql" and not self.sql:
            raise ValueError("action 为 sql 时必须提供 sql。")
        if self.action == "python" and self.python is None:
            raise ValueError("action 为 python 时必须提供 python 分析参数。")
        if self.action == "chart" and self.chart is None:
            raise ValueError("action 为 chart 时必须提供图表参数。")
        return self


class DeepSeekClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.model = settings.llm_api_id
        self.thinking = settings.llm_thinking
        self.usage_calls = []
        self.limits = LLMLimits.from_settings(settings)
        self.started = monotonic()
        self.budget_stop_reason = None
        self._phase = None
        self.client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            max_retries=0,  # Keep transport attempts observable; no hidden SDK retries.
            timeout=self.limits.timeout(self.started),
        )

    def usage_snapshot(self) -> AnalysisUsage:
        usage = AnalysisUsage.aggregate(getattr(self, "usage_calls", []))
        usage.budget_stop_reason = getattr(self, "budget_stop_reason", None)
        return usage

    def _new_call(self, messages, attempt, reason=None):
        if not hasattr(self, "usage_calls"):
            self.usage_calls = []
        if not hasattr(self, "limits"):
            self.limits = LLMLimits()
        if not hasattr(self, "started"):
            self.started = monotonic()
        error = budget_error(self.limits, self.usage_calls, messages, self.started)
        if error:
            self.budget_stop_reason = error
            raise LLMError(error, code=budget_stop_code(error))
        if (getattr(self, "_phase", None) in {"planning", "decision"}
                and len(self.usage_calls) >= max(0, self.limits.max_calls - 1)):
            self.budget_stop_reason = "已预留最后一次模型调用用于报告，停止继续规划。"
            raise LLMError(self.budget_stop_reason, code="report_reserved")
        call = ModelCallUsage(model=self.model, attempt=attempt, retry_reason=reason,
                              estimated_prompt_tokens=estimated_prompt_tokens(messages),
                              output_token_limit=self.limits.max_output_tokens,
                              phase=getattr(self, "_phase", None))
        self.usage_calls.append(call)
        return call

    def _completion(self, messages, attempt, *, streaming=False):
        """Retry only initial transport failures, never SQL or already-emitted text."""
        retries = getattr(self, "limits", LLMLimits()).network_retries
        for network_attempt in range(retries + 1):
            reason = "network" if network_attempt else "format" if attempt > 1 else None
            call = self._new_call(messages, attempt + network_attempt, reason)
            try:
                response = self.client.chat.completions.create(
                    model=self.model, messages=messages.copy(),
                    max_tokens=self.limits.max_output_tokens,
                    timeout=self.limits.timeout(self.started),
                    extra_body={"thinking": {"type": self.thinking}},
                    **({"stream": True, "stream_options": {"include_usage": True}} if streaming else
                       {"response_format": {"type": "json_object"}}),
                )
                return response, call
            except APIConnectionError as exc:
                call.status = "timeout" if isinstance(exc, APITimeoutError) else "connection_error"
                error = LLMError("模型服务等待超时。" if isinstance(exc, APITimeoutError)
                                 else "无法连接到 DeepSeek，请检查网络和 LLM_BASE_URL。",
                                 code="timeout" if isinstance(exc, APITimeoutError) else "connection_error")
            except APIStatusError as exc:
                call.status = "api_error"
                error = LLMError(f"DeepSeek API 请求失败，状态码：{exc.status_code}。", code="upstream_error")
                if exc.status_code not in {429, 500, 502, 503, 504}:
                    raise error from exc
            if network_attempt >= retries:
                raise error
            logger.warning("llm_transport_retry attempt=%s", network_attempt + 1)
            sleep(0.25 * (network_attempt + 1))

    @staticmethod
    def _record_usage(call, usage):
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, field, None)
            setattr(call, field, value if isinstance(value, int) and value >= 0 else None)
        cached = getattr(usage, "prompt_cache_hit_tokens", None)
        if cached is None:
            cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        call.cache_hit_tokens = cached if isinstance(cached, int) and cached >= 0 else None

    def stream_report(self, state, max_rounds, schema, emit):
        """Stream only a report after the graph's existing evidence gates pass."""
        self._phase = "report"
        call = None
        stream = None
        parts = []
        finish_reason = None
        try:
            messages = [
                    {"role": "system", "content": REPORT_PROMPT +
                     "\n本次是证据已通过程序核验后的最终报告阶段。不要输出决策 JSON，"
                     "不要调用工具，仅输出给用户的 Markdown 回答正文。"
                     "只使用本轮工具中的数字，历史数字不能作为本轮事实补充。"},
                    {"role": "user", "content": build_user_prompt(state, max_rounds, schema, report=True)},
                ]
            stream, call = self._completion(messages, 1, streaming=True)
            for chunk in stream:
                if monotonic() - self.started >= self.limits.request_deadline_seconds:
                    raise LLMError("本轮模型流式回答超过等待时限，本轮未完成，请重试。", code="deadline")
                if getattr(chunk, "usage", None) is not None:
                    self._record_usage(call, chunk.usage)
                if chunk.choices:
                    choice = chunk.choices[0]
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                        call.finish_reason = finish_reason
                    delta = choice.delta.content
                    if delta:
                        parts.append(delta)
                        emit("delta", {"text": delta})
            # Drain the terminal usage chunk even when the provider hit its output limit.
            if finish_reason == "length":
                call.status = "output_limit"
                raise LLMError("模型回答达到输出长度上限，文字报告未完整生成。", code="output_limit")
            if finish_reason not in (None, "stop"):
                call.status = "report_rejected"
                raise LLMError("模型未正常完成文字报告。", code="report_rejected")
            if finish_reason is None:
                raise LLMError("模型流式回答中断，本轮未完成，请重试。", code="stream_error")
            if not parts:
                raise LLMError("模型未返回回答正文，请重试。")
            call.status = "success"
            return "".join(parts)
        except (APIConnectionError, APIStatusError) as exc:
            if call:
                call.status = "timeout" if isinstance(exc, APITimeoutError) else "stream_error"
            raise LLMError("模型流式等待超时，本轮回答未完成。" if isinstance(exc, APITimeoutError)
                           else "模型流式连接失败，本轮回答未完成，请重试。",
                           code="timeout" if isinstance(exc, APITimeoutError) else "stream_error") from exc
        except LLMError as exc:
            if call and call.status == "pending":
                call.status = "timeout" if "时限" in str(exc) else "stream_error"
            raise
        except Exception as exc:
            if call:
                call.status = "stream_error"
            raise LLMError("模型流式响应中断或格式异常，本轮未完成，请重试。", code="stream_error") from exc
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    logger.warning("llm_stream_close_failed")

    def decide(
        self, state: AgentState, max_rounds: int, schema: str, *, report=False
    ) -> AgentDecision:
        self._phase = "report" if report else "planning" if state.plan is None else "decision"
        messages = [
            {"role": "system", "content": (
                REPORT_PROMPT + '\n只输出完整 JSON：{"action":"finish","reasoning":"证据核验后报告",'
                '"plan":null,"sql":null,"python":null,"chart":null,"content":"面向用户的中文报告"}'
                if report else PLANNING_PROMPT if uses_scope_planning(state) else SYSTEM_PROMPT
            )},
            {
                "role": "user",
                "content": build_user_prompt(state, max_rounds, schema, report=report),
            },
        ]
        for attempt in range(2):
            response, call = self._completion(messages, attempt + 1)

            usage = getattr(response, "usage", None)
            self._record_usage(call, usage)
            call.status = "invalid_decision"
            if response.choices:
                call.finish_reason = getattr(response.choices[0], "finish_reason", None)
                if call.finish_reason not in (None, "stop"):
                    call.status = "output_limit" if call.finish_reason == "length" else "report_rejected"
                    raise LLMError("模型输出未正常结束，未将截断内容作为完整决策或报告。",
                                   code="output_limit" if call.finish_reason == "length" else "report_rejected")
            content = response.choices[0].message.content if response.choices else None
            fields = []
            if content:
                try:
                    decision = AgentDecision.model_validate_json(content)
                    if report and decision.action != "finish":
                        call.status = "invalid_report"
                        raise LLMError("最终报告阶段返回了工具决策，已停止；不重复调用或执行新工具。", code="invalid_report")
                    call.status = "success"
                    return decision
                except ValidationError as exc:
                    errors = exc.errors(include_input=False, include_url=False)
                    fields = [".".join(map(str, error["loc"])) for error in errors]
                    validation_hint = "; ".join(
                        f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors[:5]
                    )
            else:
                validation_hint = "输出内容为空"

            if attempt == 0:
                logger.warning("llm_decision_invalid retrying_once fields=%s", fields)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上次决策缺少字段或格式不正确。请重新输出完整 JSON 对象，"
                            "严格包含 action、reasoning、plan、sql、python、chart、content；"
                            "如果这是首轮，plan 必须是完整对象。只返回 JSON。"
                            f"需要修正的格式：{validation_hint}"
                        ),
                    }
                )

        raise LLMError("DeepSeek 连续两次返回空内容或不符合 Agent 决策格式。", code="invalid_decision")
