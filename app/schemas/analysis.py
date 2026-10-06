from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.agent.state import AgentStep, AnalysisPlan
from app.agent.context import AnalysisContext, FollowupSuggestion
from app.llm.usage import AnalysisUsage
from app.agent.diagnostics import AnalysisDiagnostics


class AnalysisRequest(BaseModel):
    question: str = Field(
        min_length=2,
        max_length=2000,
        description="需要 Agent 分析的问题",
        examples=["如果销售额连续三个月下降，应该从哪些角度分析？"],
    )
    session_id: UUID | None = Field(
        default=None, description="不填则开启新会话；追问时填写上次返回的 session_id"
    )


class AnalysisResponse(BaseModel):
    answer: str
    plan: AnalysisPlan | None = None
    rounds: int
    finished: bool
    steps: list[AgentStep]
    charts: list[str] = Field(default_factory=list)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    usage: AnalysisUsage | None = None
    session_id: UUID | None = None
    analysis_context: AnalysisContext | None = None
    suggestions: list[FollowupSuggestion] = Field(default_factory=list)
    requires_clarification: bool = False
    context_reset: bool = False
    session_context: AnalysisContext | None = None
    diagnostics: AnalysisDiagnostics | None = None


class SessionHistoryItem(BaseModel):
    turn_no: int
    question: str
    answer: str | None
    status: str
    created_at: datetime
    finished_at: datetime | None
    charts: list[str] = Field(default_factory=list)
    suggestions: list[FollowupSuggestion] = Field(default_factory=list)
    analysis_context: AnalysisContext | None = None
    context_reset: bool = False
    session_context: AnalysisContext | None = None
    usage: AnalysisUsage | None = None
    diagnostics: AnalysisDiagnostics | None = None


class SessionHistoryResponse(BaseModel):
    session_id: UUID
    turns: list[SessionHistoryItem]


class SessionSummary(BaseModel):
    session_id: UUID
    title: str
    question_preview: str = ""
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
