"""Chat 编排使用的核心 Pydantic 数据模型。

这些模型位于 API schema 与数据库模型之间，描述意图识别、会话上下文
以及 Supervisor 的输入输出契约。它们不包含 SQL、ORM 字段或模型思维过程。
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from weight_agent.domain.metrics.models import MetricType


class ChatDomain(StrEnum):
    """请求所属的业务域。"""

    GENERAL = "general"
    HEALTH_DATA = "health_data"
    HEALTH_KNOWLEDGE = "health_knowledge"
    OUT_OF_SCOPE = "out_of_scope"


class ChatIntent(StrEnum):
    """Chat 的主意图。"""

    GREETING = "greeting"
    METRIC_QUERY = "metric_query"
    METRIC_ANALYSIS = "metric_analysis"
    DATA_BASED_ADVICE = "data_based_advice"
    DOMAIN_ADVICE = "domain_advice"
    OUT_OF_SCOPE = "out_of_scope"


class OutputNeed(StrEnum):
    """用户希望得到的输出类型。"""

    DATA = "data"
    COMPARISON = "comparison"
    ADVICE = "advice"


class RiskLevel(StrEnum):
    """健康风险等级。"""

    NONE = "none"
    MEDICAL_REVIEW = "medical_review"
    URGENT = "urgent"


class AdviceCategory(StrEnum):
    """建议类别。"""

    DIET = "diet"
    EXERCISE = "exercise"
    SLEEP = "sleep"
    LIFESTYLE = "lifestyle"


class ComparisonMode(StrEnum):
    """数据比较方式。"""

    PREVIOUS_PERIOD = "previous_period"
    TARGET = "target"
    NONE = "none"


class QueryEntities(BaseModel):
    """从自然语言中抽取、但尚未完成日期归一化的查询槽位。"""

    model_config = ConfigDict(extra="forbid")

    metrics: list[MetricType] = Field(default_factory=list, max_length=8)
    time_expression: str | None = Field(default=None, max_length=128)
    comparison_mode: ComparisonMode = ComparisonMode.NONE
    advice_categories: list[AdviceCategory] = Field(default_factory=list, max_length=4)
    goal: str | None = Field(default=None, max_length=256)


class IntentResult(BaseModel):
    """意图识别器的结构化输出。"""

    model_config = ConfigDict(extra="forbid")

    domain: ChatDomain
    intent: ChatIntent
    output_needs: list[OutputNeed] = Field(default_factory=list, max_length=3)
    entities: QueryEntities = Field(default_factory=QueryEntities)
    confidence: float = Field(ge=0, le=1)
    risk_level: RiskLevel = RiskLevel.NONE
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=256)
    reason_codes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def clarification_requires_question(self) -> "IntentResult":
        """需要澄清时必须给出一个面向用户的问题。"""
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("clarification_question is required when needs_clarification is true")
        return self


class AnalysisResult(BaseModel):
    """确定性数据分析服务输出的最小事实模型。"""

    model_config = ConfigDict(extra="forbid")

    metric: MetricType
    observation_count: int = Field(ge=0)
    first_value: Decimal | None = None
    last_value: Decimal | None = None
    minimum_value: Decimal | None = None
    maximum_value: Decimal | None = None
    average_value: Decimal | None = None
    absolute_change: Decimal | None = None
    change_rate: Decimal | None = None
    trend: Literal["up", "down", "stable", "insufficient_data"] = "insufficient_data"
    period_start: datetime
    period_end: datetime
    baseline_period_start: datetime | None = None
    baseline_period_end: datetime | None = None
    data_coverage: float | None = Field(default=None, ge=0, le=1)


class AdviceFact(BaseModel):
    """供业务建议 Agent 使用的可追溯事实。"""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1, max_length=512)
    evidence: list[str] = Field(default_factory=list, max_length=8)
    risk_level: RiskLevel = RiskLevel.NONE


class AdviceFacts(BaseModel):
    """分析结果转换后的建议事实集合。"""

    model_config = ConfigDict(extra="forbid")

    facts: list[AdviceFact] = Field(default_factory=list, max_length=32)
    categories: list[AdviceCategory] = Field(default_factory=list, max_length=4)
    rule_version: str = Field(default="v1", min_length=1, max_length=32)


class ConversationTurn(BaseModel):
    """会话中的一轮最终消息。"""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)
    created_at: datetime


class ConversationContext(BaseModel):
    """带 TTL 的短期会话上下文。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    summary: str | None = Field(default=None, max_length=2000)
    recent_turns: list[ConversationTurn] = Field(default_factory=list, max_length=20)
    confirmed_entities: QueryEntities | None = None
    last_intent: ChatIntent | None = None
    last_analysis: AnalysisResult | None = None
    expires_at: datetime


class SupervisorInput(BaseModel):
    """Chat Supervisor 的结构化输入。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
    timezone: str = Field(min_length=1, max_length=64)
    locale: str = Field(default="zh-CN", min_length=1, max_length=16)
    conversation_summary: str | None = Field(default=None, max_length=2000)
    recent_turns: list[ConversationTurn] = Field(default_factory=list, max_length=20)
    confirmed_entities: QueryEntities | None = None
    last_analysis: AnalysisResult | None = None


SupervisorRoute = Literal[
    "greeting",
    "data_analysis",
    "data_based_advice",
    "domain_advice",
    "safety",
    "out_of_scope",
    "clarification",
]
RequiredAgent = Literal["data_analysis", "business_advice"]


class SupervisorPlan(BaseModel):
    """Supervisor 生成并经确定性护栏校验的执行计划。"""

    model_config = ConfigDict(extra="forbid")

    route: SupervisorRoute
    intent_result: IntentResult
    required_agents: list[RequiredAgent] = Field(default_factory=list, max_length=2)
    requires_metric_query: bool = False
    clarification_question: str | None = Field(default=None, max_length=256)
    safety_message: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_route_contract(self) -> "SupervisorPlan":
        """确保计划不会绕过安全边界或访问不必要的数据。"""
        expected_agents: dict[SupervisorRoute, list[RequiredAgent]] = {
            "greeting": [],
            "data_analysis": ["data_analysis"],
            "data_based_advice": ["data_analysis", "business_advice"],
            "domain_advice": ["business_advice"],
            "safety": [],
            "out_of_scope": [],
            "clarification": [],
        }
        if self.required_agents != expected_agents[self.route]:
            raise ValueError(f"required_agents do not match route {self.route}")
        expected_metric_query = self.route in {"data_analysis", "data_based_advice"}
        if self.requires_metric_query != expected_metric_query:
            raise ValueError(f"requires_metric_query does not match route {self.route}")
        expected_intents: dict[SupervisorRoute, set[ChatIntent]] = {
            "greeting": {ChatIntent.GREETING},
            "data_analysis": {ChatIntent.METRIC_QUERY, ChatIntent.METRIC_ANALYSIS},
            "data_based_advice": {ChatIntent.DATA_BASED_ADVICE},
            "domain_advice": {ChatIntent.DOMAIN_ADVICE},
            "safety": {
                ChatIntent.METRIC_QUERY,
                ChatIntent.METRIC_ANALYSIS,
                ChatIntent.DATA_BASED_ADVICE,
                ChatIntent.DOMAIN_ADVICE,
            },
            "out_of_scope": {ChatIntent.OUT_OF_SCOPE},
            "clarification": {
                ChatIntent.METRIC_QUERY,
                ChatIntent.METRIC_ANALYSIS,
                ChatIntent.DATA_BASED_ADVICE,
                ChatIntent.DOMAIN_ADVICE,
                ChatIntent.GREETING,
                ChatIntent.OUT_OF_SCOPE,
            },
        }
        if self.intent_result.intent not in expected_intents[self.route]:
            raise ValueError(f"intent_result.intent does not match route {self.route}")
        if self.route == "clarification" and not self.clarification_question:
            raise ValueError("clarification_question is required for clarification route")
        if self.route == "clarification" and not self.intent_result.needs_clarification:
            raise ValueError("clarification route requires intent_result.needs_clarification")
        if self.route == "safety" and not self.safety_message:
            raise ValueError("safety_message is required for safety route")
        if self.route == "safety" and self.intent_result.risk_level is RiskLevel.NONE:
            raise ValueError("safety route requires a non-none risk_level")
        return self
