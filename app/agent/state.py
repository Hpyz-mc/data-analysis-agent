from pydantic import BaseModel, Field, model_validator
from typing import Literal

from app.agent.analysis_policy import AnalysisTask
from app.agent.context import AnalysisContext


class AnalysisPlan(BaseModel):
    question_type: Literal["concept", "lookup", "comparison", "diagnosis"]
    requires_sql: bool
    metric: str
    time_range: str
    comparison: str
    dimensions: list[str] = Field(default_factory=list)
    analysis_steps: list[str] = Field(min_length=1)
    python_operation: Literal[
        "summary", "percent_change", "share", "profile", "attribution"
    ] | None = None
    chart_type: Literal["line", "bar"] | None = None
    analysis_context: AnalysisContext | None = None
    answer_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_data_requirements(self) -> "AnalysisPlan":
        if self.question_type != "concept" and not self.requires_sql:
            raise ValueError("数据查询、对比或诊断必须查询 SQL。")
        if self.python_operation and not self.requires_sql:
            raise ValueError("Python 分析必须先有 SQL 数据。")
        if self.chart_type and not self.requires_sql:
            raise ValueError("图表必须先有 SQL 数据。")
        return self


class AgentStep(BaseModel):
    round: int = Field(ge=1)
    action: Literal["sql", "python", "chart"]
    reasoning: str
    tool_input: str
    observation: str


class ConversationTurn(BaseModel):
    question: str
    answer: str


class AgentState(BaseModel):
    question: str
    task: AnalysisTask = Field(default_factory=AnalysisTask)
    conversation_history: list[ConversationTurn] = Field(default_factory=list)
    plan: AnalysisPlan | None = None
    current_round: int = 0
    finished: bool = False
    steps: list[AgentStep] = Field(default_factory=list)
    charts: list[str] = Field(default_factory=list)
    feedback: str = ""
    analysis_context: AnalysisContext | None = None
