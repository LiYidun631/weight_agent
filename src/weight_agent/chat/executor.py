"""Chat 路由执行器：按 SupervisorPlan 调用受控节点。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from weight_agent.chat.models import SupervisorPlan
from weight_agent.chat.node import (
    BusinessAdviceNode,
    ChatNode,
    ClarificationReplyNode,
    DataAnalysisNode,
    GreetingReplyNode,
    NodeContext,
    NodeResult,
    SafetyReplyNode,
    ScopeReplyNode,
)
from weight_agent.domain.metrics.repository import MetricRepository


class RouteExecutionResult(BaseModel):
    """一次路由执行的统一结果。"""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=8000)
    timezone: str = Field(min_length=1, max_length=64)
    node_results: list[NodeResult] = Field(min_length=1, max_length=4)
    data: dict[str, Any] = Field(default_factory=dict)


class RouteExecutor:
    """根据 SupervisorPlan 执行固定路由。

    该执行器不重新判断意图，也不允许节点自行改变路由。节点依赖通过构造函数注入，
    便于离线测试以及后续替换为真实的数据分析和业务建议实现。
    """

    def __init__(
        self,
        *,
        greeting: ChatNode | None = None,
        clarification: ChatNode | None = None,
        safety: ChatNode | None = None,
        scope: ChatNode | None = None,
        data_analysis: ChatNode | None = None,
        business_advice: ChatNode | None = None,
        metric_repository: MetricRepository | None = None,
    ) -> None:
        data_analysis_node = data_analysis or DataAnalysisNode(repository=metric_repository)
        business_advice_node = business_advice or BusinessAdviceNode()
        self._nodes: dict[str, ChatNode] = {
            "greeting": greeting or GreetingReplyNode(),
            "clarification": clarification or ClarificationReplyNode(),
            "safety": safety or SafetyReplyNode(),
            "out_of_scope": scope or ScopeReplyNode(),
            "data_analysis": data_analysis_node,
            "business_advice": business_advice_node,
            "domain_advice": business_advice_node,
        }

    async def execute(
        self,
        plan: SupervisorPlan,
        context: NodeContext,
    ) -> RouteExecutionResult:
        """按计划执行单节点或固定顺序的多节点路由。"""
        if plan.route == "data_based_advice":
            return await self._execute_data_based_advice(plan, context)

        node = self._nodes.get(plan.route)
        if node is None:
            raise ValueError(f"unsupported supervisor route: {plan.route}")

        result = await node.execute(plan, context)
        return self._aggregate(plan.route, [result], context)

    async def _execute_data_based_advice(
        self,
        plan: SupervisorPlan,
        context: NodeContext,
    ) -> RouteExecutionResult:
        analysis_result = await self._nodes["data_analysis"].execute(plan, context)
        if analysis_result.status != "completed":
            return self._aggregate(plan.route, [analysis_result], context)
        analysis_facts = analysis_result.data.get("analysis_results", [])
        advice_context = context.model_copy(
            update={"artifacts": {**context.artifacts, "analysis_results": analysis_facts}}
        )
        advice_result = await self._nodes["business_advice"].execute(plan, advice_context)
        return self._aggregate(plan.route, [analysis_result, advice_result], context)

    @staticmethod
    def _aggregate(
        route: str,
        results: list[NodeResult],
        context: NodeContext,
    ) -> RouteExecutionResult:
        final_result = results[-1]
        return RouteExecutionResult(
            route=route,
            status=final_result.status,
            content=final_result.content,
            timezone=context.timezone,
            node_results=results,
            data={
                "executed_nodes": [result.node_name for result in results],
                "node_data": {
                    result.node_name: dict(result.data)
                    for result in results
                },
            },
        )
