import asyncio

import pytest

from weight_agent.chat.models import (
    ChatDomain,
    ChatIntent,
    IntentResult,
    OutputNeed,
    RiskLevel,
)
from weight_agent.chat.node import (
    BusinessAdviceNode,
    ChatNode,
    ClarificationReplyNode,
    DataAnalysisNode,
    GreetingReplyNode,
    NodeContext,
    NodeRouteError,
    SafetyReplyNode,
    ScopeReplyNode,
)
from weight_agent.chat.supervisor import ChatSupervisor

CONTEXT = NodeContext(
    request_id="req-1",
    conversation_id="conv-1",
    user_id="user-1",
    timezone="UTC",
)


def make_plan(intent: IntentResult):
    return ChatSupervisor().build_plan(intent)


def test_nodes_follow_structural_protocol() -> None:
    assert isinstance(GreetingReplyNode(), ChatNode)


def test_greeting_reply_node_returns_template() -> None:
    plan = make_plan(
        IntentResult(
            domain=ChatDomain.GENERAL,
            intent=ChatIntent.GREETING,
            confidence=0.96,
        )
    )

    result = asyncio.run(GreetingReplyNode().execute(plan, CONTEXT))

    assert result.node_name == "greeting_reply"
    assert result.route == "greeting"
    assert "体重管理" in result.content


def test_clarification_reply_node_returns_needs_clarification() -> None:
    plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_QUERY,
            confidence=0.64,
            needs_clarification=True,
            clarification_question="你想查询哪项指标？",
        )
    )

    result = asyncio.run(ClarificationReplyNode().execute(plan, CONTEXT))

    assert result.status == "needs_clarification"
    assert result.content == "你想查询哪项指标？"


def test_safety_and_scope_reply_nodes_return_templates() -> None:
    safety_plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_KNOWLEDGE,
            intent=ChatIntent.DOMAIN_ADVICE,
            output_needs=[OutputNeed.ADVICE],
            confidence=0.99,
            risk_level=RiskLevel.URGENT,
        )
    )
    scope_plan = make_plan(
        IntentResult(
            domain=ChatDomain.OUT_OF_SCOPE,
            intent=ChatIntent.OUT_OF_SCOPE,
            confidence=0.98,
        )
    )

    safety_result = asyncio.run(SafetyReplyNode().execute(safety_plan, CONTEXT))
    scope_result = asyncio.run(ScopeReplyNode().execute(scope_plan, CONTEXT))

    assert "就医" in safety_result.content
    assert safety_result.status == "blocked"
    assert "超出了" in scope_result.content


def test_business_advice_node_returns_structured_domain_advice() -> None:
    data_plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_ANALYSIS,
            output_needs=[OutputNeed.DATA, OutputNeed.COMPARISON],
            confidence=0.9,
            entities={"metrics": ["weight"], "time_expression": "最近30天"},
        )
    )
    advice_plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_KNOWLEDGE,
            intent=ChatIntent.DOMAIN_ADVICE,
            output_needs=[OutputNeed.ADVICE],
            confidence=0.9,
        )
    )

    analysis_result = asyncio.run(DataAnalysisNode().execute(data_plan, CONTEXT))
    advice_result = asyncio.run(BusinessAdviceNode().execute(advice_plan, CONTEXT))

    assert analysis_result.data["execution_status"] == "query_plan_only"
    assert analysis_result.data["requires_metric_query"] is True
    assert advice_result.data["requires_analysis"] is False
    assert advice_result.data["execution_status"] == "completed"
    assert advice_result.data["advice"]["recommendations"]
    assert "稳定饮食结构" in advice_result.content


def test_data_analysis_node_builds_metric_query_from_time_expression() -> None:
    plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_ANALYSIS,
            output_needs=[OutputNeed.DATA, OutputNeed.COMPARISON],
            confidence=0.9,
            entities={"metrics": ["weight"], "time_expression": "最近30天"},
        )
    )

    result = asyncio.run(DataAnalysisNode().execute(plan, CONTEXT))

    assert result.status == "completed"
    assert result.data["metric_query"]["user_id"] == "user-1"
    assert result.data["metric_query"]["metrics"] == ["weight"]
    assert result.data["time_resolution"]["reason_codes"] == ["rule_time_match"]


def test_data_analysis_node_clarifies_when_metric_is_missing() -> None:
    plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_QUERY,
            confidence=0.9,
        )
    )

    result = asyncio.run(DataAnalysisNode().execute(plan, CONTEXT))

    assert result.status == "needs_clarification"
    assert result.data["reason"] == "metric_required"


def test_data_analysis_node_preserves_latest_count_and_timezone() -> None:
    plan = make_plan(
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_QUERY,
            confidence=0.9,
            entities={"metrics": ["weight"], "time_expression": "最近三次"},
        )
    )

    result = asyncio.run(DataAnalysisNode().execute(plan, CONTEXT))

    assert result.status == "completed"
    assert result.data["latest_count"] == 3
    assert result.data["timezone"] == "UTC"
    assert result.data["latest_query"]["metrics"] == ["weight"]


def test_node_rejects_unsupported_route() -> None:
    plan = make_plan(
        IntentResult(
            domain=ChatDomain.GENERAL,
            intent=ChatIntent.GREETING,
            confidence=0.96,
        )
    )

    with pytest.raises(NodeRouteError, match="does not support route"):
        asyncio.run(DataAnalysisNode().execute(plan, CONTEXT))
