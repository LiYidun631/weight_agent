"""Chat 执行节点协议、统一输出和一期节点实现。

节点只负责一个受控步骤，不负责修改 SupervisorPlan，也不自行决定下一条路由。
后续 RouteExecutor 可以根据 ``SupervisorPlan.route`` 调用这些节点。
"""

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from weight_agent.chat.advice import (
    HealthAdviceAgent,
    HealthAdviceRequest,
    RuleFirstHealthAdviceAgent,
)
from weight_agent.chat.models import (
    AdviceFacts,
    AnalysisResult,
    ConversationTurn,
    SupervisorPlan,
    SupervisorRoute,
)
from weight_agent.domain.metrics.analysis import MetricAnalysisService
from weight_agent.domain.metrics.models import MetricQuery
from weight_agent.domain.metrics.repository import MetricRepository
from weight_agent.domain.time.resolver import TimeRangeResolver


class NodeContext(BaseModel):
    """节点执行所需的服务端上下文。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=4000)
    locale: str = Field(default="zh-CN", min_length=1, max_length=16)
    detected_language: str = Field(default="zh", min_length=2, max_length=16)
    response_language: str = Field(default="zh-CN", min_length=2, max_length=16)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    conversation_summary: str | None = Field(default=None, max_length=2000)
    recent_turns: list[ConversationTurn] = Field(default_factory=list, max_length=10)
    artifacts: dict[str, Any] = Field(default_factory=dict)


class NodeResult(BaseModel):
    """节点返回的统一结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    node_name: str = Field(min_length=1, max_length=64)
    route: SupervisorRoute
    status: Literal["completed", "needs_clarification", "needs_data", "blocked"] = "completed"
    content: str = Field(min_length=1, max_length=8000)
    data: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ChatNode(Protocol):
    """所有 Chat 节点的统一异步接口。"""

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        """根据执行计划完成一个受控节点步骤。"""


class NodeRouteError(ValueError):
    """节点收到不属于自身职责的路由计划。"""


class _RouteCheckedNode:
    """提供节点路由校验的内部基类。"""

    node_name: str
    supported_routes: frozenset[SupervisorRoute]

    def _check_route(self, plan: SupervisorPlan) -> None:
        if plan.route not in self.supported_routes:
            supported = ", ".join(sorted(self.supported_routes))
            raise NodeRouteError(
                f"{self.node_name} does not support route {plan.route}; "
                f"supported routes: {supported}"
            )

    def _result(
        self,
        *,
        plan: SupervisorPlan,
        content: str,
        data: Mapping[str, Any] | None = None,
        status: Literal["completed", "needs_clarification", "needs_data", "blocked"] = "completed",
    ) -> NodeResult:
        return NodeResult(
            node_name=self.node_name,
            route=plan.route,
            status=status,
            content=content,
            data=dict(data or {}),
        )


class GreetingReplyNode(_RouteCheckedNode):
    """处理纯问候、能力介绍和使用引导。"""

    node_name = "greeting_reply"
    supported_routes = frozenset({"greeting"})

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        return self._result(
            plan=plan,
            content=(
                "你好，我可以帮你查询和分析体重、BMI、体脂率、腰围等数据，"
                "也可以提供体重管理相关的饮食、运动和生活方式建议。"
            ),
        )


class ClarificationReplyNode(_RouteCheckedNode):
    """处理意图或关键参数不足的澄清请求。"""

    node_name = "clarification_reply"
    supported_routes = frozenset({"clarification"})

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        question = plan.clarification_question
        if not question:
            raise NodeRouteError("clarification plan must contain clarification_question")
        return self._result(
            plan=plan,
            content=question,
            status="needs_clarification",
        )


class SafetyReplyNode(_RouteCheckedNode):
    """处理急症和需要医疗谨慎的请求。"""

    node_name = "safety_reply"
    supported_routes = frozenset({"safety"})

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        message = plan.safety_message
        if not message:
            raise NodeRouteError("safety plan must contain safety_message")
        # 安全回复不是正常完成，用 blocked 让客户端与后续轮次区分对待
        return self._result(plan=plan, content=message, status="blocked")


class ScopeReplyNode(_RouteCheckedNode):
    """处理体重管理业务范围外的问题。"""

    node_name = "scope_reply"
    supported_routes = frozenset({"out_of_scope"})

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        return self._result(
            plan=plan,
            content="这个问题超出了体重管理和健康生活方式咨询范围。",
        )


class DataAnalysisNode(_RouteCheckedNode):
    """数据分析节点的占位实现。

    后续在这里接入时间归一化、MetricRepository 和确定性分析服务。
    """

    node_name = "data_analysis"
    supported_routes = frozenset({"data_analysis", "data_based_advice"})

    def __init__(
        self,
        repository: MetricRepository | None = None,
        time_resolver: TimeRangeResolver | None = None,
        analysis_service: MetricAnalysisService | None = None,
    ) -> None:
        """注入指标仓库；数据库未接入时允许为空并返回占位结果。"""
        self._repository = repository
        self._time_resolver = time_resolver or TimeRangeResolver()
        self._analysis_service = analysis_service or MetricAnalysisService()

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        entities = plan.intent_result.entities
        if not entities.metrics:
            return self._result(
                plan=plan,
                content="你想查询体重、BMI、体脂率还是腰围？",
                status="needs_clarification",
                data={"reason": "metric_required"},
            )
        resolved_time = self._time_resolver.resolve(
            entities.time_expression,
            timezone=context.timezone,
            comparison_mode=entities.comparison_mode,
        )
        if resolved_time.latest_count is not None:
            return self._result(
                plan=plan,
                content="数据分析节点已识别最近记录数量，真实记录查询将在下一阶段接入。",
                data={
                    "execution_status": "not_implemented",
                    "repository_configured": self._repository is not None,
                    "timezone": context.timezone,
                    "time_resolution": resolved_time.model_dump(mode="json"),
                    "latest_count": resolved_time.latest_count,
                    "latest_query": {
                        "user_id": context.user_id,
                        "metrics": [metric.value for metric in entities.metrics],
                        "before": None,
                    },
                },
            )
        if resolved_time.needs_clarification or resolved_time.current is None:
            return self._result(
                plan=plan,
                content=resolved_time.clarification_question or "请补充明确的时间范围。",
                status="needs_clarification",
                data={"time_resolution": resolved_time.model_dump(mode="json")},
            )
        query = MetricQuery(
            user_id=context.user_id,
            metrics=set(entities.metrics),
            start_at=resolved_time.current.start_at,
            end_at=resolved_time.current.end_at,
            timezone=context.timezone,
        )
        observations = []
        metric_result = None
        analysis_results = []
        if self._repository is not None:
            metric_result = await self._repository.get_observations(query)
            observations = metric_result.observations
            analysis_results = self._analysis_service.analyze(
                observations,
                period=resolved_time,
            )
        return self._result(
            plan=plan,
            content=(
                "已完成指标查询和分析。"
                if metric_result
                else "已生成指标查询计划，数据仓库尚未配置。"
            ),
            data={
                "execution_status": "completed" if metric_result else "query_plan_only",
                "requires_metric_query": plan.requires_metric_query,
                "repository_configured": self._repository is not None,
                "timezone": context.timezone,
                "time_resolution": resolved_time.model_dump(mode="json"),
                "metric_query": query.model_dump(mode="json"),
                "metric_result": metric_result.model_dump(mode="json") if metric_result else None,
                "analysis_results": [item.model_dump(mode="json") for item in analysis_results],
            },
        )


class BusinessAdviceNode(_RouteCheckedNode):
    """调用健康建议 Agent 的业务建议节点。"""

    node_name = "business_advice"
    supported_routes = frozenset({"domain_advice", "data_based_advice"})

    def __init__(self, agent: HealthAdviceAgent | None = None) -> None:
        self._agent = agent or RuleFirstHealthAdviceAgent()

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        analysis_results, invalid_analysis_count = self._parse_analysis_results(
            context.artifacts.get("analysis_results", [])
        )
        advice_facts = self._parse_advice_facts(context.artifacts.get("advice_facts"))
        message = context.message.strip() or "请给我一些体重管理建议"
        response = await self._agent.advise(
            HealthAdviceRequest(
                message=message,
                mode="data_based" if plan.route == "data_based_advice" else "domain",
                categories=plan.intent_result.entities.advice_categories,
                goal=plan.intent_result.entities.goal,
                timezone=context.timezone,
                locale=context.locale,
                risk_level=plan.intent_result.risk_level,
                conversation_summary=context.conversation_summary,
                recent_turns=context.recent_turns,
                analysis_results=analysis_results,
                advice_facts=advice_facts,
            )
        )
        return self._result(
            plan=plan,
            content=response.render_text(),
            data={
                "execution_status": response.status,
                "requires_analysis": plan.route == "data_based_advice",
                "analysis_results": [
                    item.model_dump(mode="json") for item in analysis_results
                ],
                "advice": response.model_dump(mode="json"),
                "invalid_analysis_count": invalid_analysis_count,
            },
            status=response.status,
        )

    @staticmethod
    def _parse_analysis_results(
        raw_results: Any,
    ) -> tuple[list[AnalysisResult], int]:
        if not isinstance(raw_results, list):
            return [], 1 if raw_results else 0
        parsed: list[AnalysisResult] = []
        invalid_count = 0
        for item in raw_results:
            if isinstance(item, AnalysisResult):
                parsed.append(item)
                continue
            try:
                parsed.append(AnalysisResult.model_validate(item))
            except (TypeError, ValueError):
                invalid_count += 1
        return parsed, invalid_count

    @staticmethod
    def _parse_advice_facts(raw_facts: Any) -> AdviceFacts | None:
        if raw_facts is None:
            return None
        if isinstance(raw_facts, AdviceFacts):
            return raw_facts
        try:
            return AdviceFacts.model_validate(raw_facts)
        except (TypeError, ValueError):
            return None
