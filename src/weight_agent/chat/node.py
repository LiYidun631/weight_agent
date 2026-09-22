"""Chat 执行节点协议、统一输出和一期节点实现。

节点只负责一个受控步骤，不负责修改 SupervisorPlan，也不自行决定下一条路由。
后续 RouteExecutor 可以根据 ``SupervisorPlan.route`` 调用这些节点。
"""

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from weight_agent.chat.models import SupervisorPlan, SupervisorRoute
from weight_agent.domain.metrics.models import MetricQuery
from weight_agent.domain.metrics.repository import MetricRepository
from weight_agent.domain.time.resolver import TimeRangeResolver


class NodeContext(BaseModel):
    """节点执行所需的服务端上下文。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)


class NodeResult(BaseModel):
    """节点返回的统一结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    node_name: str = Field(min_length=1, max_length=64)
    route: SupervisorRoute
    status: Literal["completed", "needs_clarification"] = "completed"
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
        status: Literal["completed", "needs_clarification"] = "completed",
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
        return self._result(plan=plan, content=message)


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
    ) -> None:
        """注入指标仓库；数据库未接入时允许为空并返回占位结果。"""
        self._repository = repository
        self._time_resolver = time_resolver or TimeRangeResolver()

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
        return self._result(
            plan=plan,
            content="数据分析节点已接收执行计划，真实指标查询和分析将在下一阶段接入。",
            data={
                "execution_status": "not_implemented",
                "requires_metric_query": plan.requires_metric_query,
                "repository_configured": self._repository is not None,
                "timezone": context.timezone,
                "time_resolution": resolved_time.model_dump(mode="json"),
                "metric_query": query.model_dump(mode="json"),
            },
        )


class BusinessAdviceNode(_RouteCheckedNode):
    """业务建议节点的占位实现。

    ``domain_advice`` 后续直接接收领域知识上下文；``data_based_advice`` 后续接收
    DataAnalysisNode 产出的 AnalysisResult/AdviceFacts。
    """

    node_name = "business_advice"
    supported_routes = frozenset({"domain_advice", "data_based_advice"})

    async def execute(self, plan: SupervisorPlan, context: NodeContext) -> NodeResult:
        self._check_route(plan)
        return self._result(
            plan=plan,
            content="业务建议节点已接收执行计划，真实建议生成将在下一阶段接入。",
            data={
                "execution_status": "not_implemented",
                "requires_analysis": plan.route == "data_based_advice",
            },
        )
