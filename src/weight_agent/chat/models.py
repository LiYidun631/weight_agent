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


class TaskType(StrEnum):
    """内部语义层的任务类型。

    Task 表示用户想完成的动作，不等同于最终路由。它用于解决“提到指标”
    与“想要建议/查询/分析”之间的语义冲突。
    """

    GREETING = "greeting"
    QUERY = "query"
    ANALYSIS = "analysis"
    ADVICE = "advice"
    COMPARE = "compare"
    EXPLAIN = "explain"
    CLARIFY = "clarify"
    OUT_OF_SCOPE = "out_of_scope"


class TopicType(StrEnum):
    """内部语义层的业务主题。"""

    WEIGHT = "weight"
    BMI = "bmi"
    BODY_FAT = "body_fat"
    WAIST = "waist"
    DIET = "diet"
    EXERCISE = "exercise"
    SLEEP = "sleep"
    LIFESTYLE = "lifestyle"
    GENERAL_HEALTH = "general_health"


class GoalType(StrEnum):
    """用户健康管理目标。"""

    WEIGHT_LOSS = "weight_loss"
    WEIGHT_GAIN = "weight_gain"
    FAT_LOSS = "fat_loss"
    MUSCLE_GAIN = "muscle_gain"
    MAINTAIN_WEIGHT = "maintain_weight"
    IMPROVE_LIFESTYLE = "improve_lifestyle"


class DataDependency(StrEnum):
    """完成当前请求是否依赖个人指标数据。"""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class SlotSource(StrEnum):
    """槽位值来源。"""

    USER = "user"
    RULE = "rule"
    LLM = "llm"
    CONTEXT = "context"
    DEFAULT = "default"


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


class SlotValue(BaseModel):
    """内部语义层的槽位值及其元信息。

    与 QueryEntities 不同，SlotValue 会记录来源、置信度、确认状态和冲突状态，
    用于多轮对话、候选融合和澄清决策。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    value: str | list[str] | None = None
    canonical_value: str | list[str] | None = None
    source: SlotSource
    confidence: float = Field(ge=0, le=1)
    confirmed: bool = False
    required: bool = False
    conflict: bool = False


class IntentCandidate(BaseModel):
    """内部语义层的候选意图。

    候选意图用于表达歧义和多意图排序，最终仍会适配成一个兼容的 IntentResult。
    """

    model_config = ConfigDict(extra="forbid")

    task: TaskType
    intent: ChatIntent
    topics: list[TopicType] = Field(default_factory=list, max_length=8)
    goals: list[GoalType] = Field(default_factory=list, max_length=4)
    data_dependency: DataDependency = DataDependency.NONE
    score: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=16)
    missing_slots: list[str] = Field(default_factory=list, max_length=8)


class SemanticParse(BaseModel):
    """意图识别内部语义解析结果。

    SemanticParse 是规则解析、LLM 解析和候选融合之间的中间表示，不直接作为
    Chat API 输出。它会在最后适配为 IntentResult 供 Supervisor 使用。
    """

    model_config = ConfigDict(extra="forbid")

    domain: ChatDomain
    task: TaskType
    topics: list[TopicType] = Field(default_factory=list, max_length=8)
    goals: list[GoalType] = Field(default_factory=list, max_length=4)
    data_dependency: DataDependency = DataDependency.NONE
    slots: list[SlotValue] = Field(default_factory=list, max_length=32)
    candidates: list[IntentCandidate] = Field(default_factory=list, max_length=8)
    selected_candidate_index: int | None = Field(default=None, ge=0)
    confidence: float = Field(ge=0, le=1)
    risk_level: RiskLevel = RiskLevel.NONE
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=256)
    reason_codes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_semantic_parse(self) -> "SemanticParse":
        """校验澄清问题和候选选择下标。"""
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("clarification_question is required when needs_clarification is true")
        if self.selected_candidate_index is not None and (
            self.selected_candidate_index >= len(self.candidates)
        ):
            raise ValueError("selected_candidate_index is out of candidates range")
        return self


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
    metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=32,
    )


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
    # 上一轮澄清未完成的槽位；非 None 表示存在待用户回答的澄清问题
    pending_entities: QueryEntities | None = None
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


class DialogueState(BaseModel):
    """内部对话状态跟踪模型。

    该模型用于后续 DST 设计，当前先作为内部结构沉淀，不替代现有
    ConversationContext.confirmed_entities/pending_entities。
    """

    model_config = ConfigDict(extra="forbid")

    active_task: TaskType | None = None
    active_topics: list[TopicType] = Field(default_factory=list, max_length=8)
    active_goal: GoalType | None = None
    confirmed_slots: list[SlotValue] = Field(default_factory=list, max_length=32)
    pending_slots: list[SlotValue] = Field(default_factory=list, max_length=16)
    last_candidates: list[IntentCandidate] = Field(default_factory=list, max_length=8)
    last_route: SupervisorRoute | None = None
    clarification_attempts: int = Field(default=0, ge=0, le=5)
    last_user_correction: bool = False


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
