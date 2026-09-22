import asyncio

from weight_agent.chat.executor import RouteExecutor
from weight_agent.chat.models import (
    ChatDomain,
    ChatIntent,
    IntentResult,
    OutputNeed,
    RiskLevel,
)
from weight_agent.chat.node import NodeContext
from weight_agent.chat.supervisor import ChatSupervisor
from weight_agent.domain.metrics.memory import InMemoryMetricRepository

CONTEXT = NodeContext(
    request_id="req-1",
    conversation_id="conv-1",
    user_id="user-1",
    timezone="UTC",
)


def plan_for(intent: IntentResult):
    return ChatSupervisor().build_plan(intent)


def test_executor_runs_greeting_route() -> None:
    plan = plan_for(
        IntentResult(
            domain=ChatDomain.GENERAL,
            intent=ChatIntent.GREETING,
            confidence=0.96,
        )
    )

    result = asyncio.run(RouteExecutor().execute(plan, CONTEXT))

    assert result.route == "greeting"
    assert result.status == "completed"
    assert result.timezone == "UTC"
    assert result.data["executed_nodes"] == ["greeting_reply"]


def test_executor_runs_clarification_route() -> None:
    plan = plan_for(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_QUERY,
            confidence=0.64,
            needs_clarification=True,
            clarification_question="你想查询哪项指标？",
        )
    )

    result = asyncio.run(RouteExecutor().execute(plan, CONTEXT))

    assert result.status == "needs_clarification"
    assert result.content == "你想查询哪项指标？"
    assert result.data["executed_nodes"] == ["clarification_reply"]


def test_executor_runs_data_based_advice_in_fixed_order() -> None:
    plan = plan_for(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.DATA_BASED_ADVICE,
            output_needs=[OutputNeed.DATA, OutputNeed.ADVICE],
            confidence=0.91,
        )
    )

    result = asyncio.run(RouteExecutor().execute(plan, CONTEXT))

    assert result.route == "data_based_advice"
    assert result.data["executed_nodes"] == ["data_analysis", "business_advice"]
    assert len(result.node_results) == 2


def test_executor_accepts_metric_repository_injection() -> None:
    plan = plan_for(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_QUERY,
            output_needs=[OutputNeed.DATA],
            confidence=0.92,
            entities={"metrics": ["weight"], "time_expression": "最近30天"},
        )
    )

    result = asyncio.run(
        RouteExecutor(metric_repository=InMemoryMetricRepository()).execute(plan, CONTEXT)
    )

    assert result.node_results[0].data["repository_configured"] is True
    assert result.node_results[0].data["execution_status"] == "not_implemented"


def test_executor_runs_safety_and_scope_routes() -> None:
    safety_plan = plan_for(
        IntentResult(
            domain=ChatDomain.HEALTH_KNOWLEDGE,
            intent=ChatIntent.DOMAIN_ADVICE,
            output_needs=[OutputNeed.ADVICE],
            confidence=0.99,
            risk_level=RiskLevel.URGENT,
        )
    )
    scope_plan = plan_for(
        IntentResult(
            domain=ChatDomain.OUT_OF_SCOPE,
            intent=ChatIntent.OUT_OF_SCOPE,
            confidence=0.98,
        )
    )

    safety_result = asyncio.run(RouteExecutor().execute(safety_plan, CONTEXT))
    scope_result = asyncio.run(RouteExecutor().execute(scope_plan, CONTEXT))

    assert safety_result.data["executed_nodes"] == ["safety_reply"]
    assert scope_result.data["executed_nodes"] == ["scope_reply"]
