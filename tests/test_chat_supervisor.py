import pytest
from pydantic import ValidationError

from weight_agent.chat.models import (
    ChatDomain,
    ChatIntent,
    IntentResult,
    OutputNeed,
    RiskLevel,
    SupervisorPlan,
)
from weight_agent.chat.supervisor import ChatSupervisor


def test_supervisor_routes_greeting_without_agents() -> None:
    intent = IntentResult(
        domain=ChatDomain.GENERAL,
        intent=ChatIntent.GREETING,
        confidence=0.96,
    )

    plan = ChatSupervisor().build_plan(intent)

    assert plan.route == "greeting"
    assert plan.required_agents == []
    assert plan.requires_metric_query is False


def test_supervisor_routes_metric_query_to_data_analysis() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_DATA,
        intent=ChatIntent.METRIC_QUERY,
        output_needs=[OutputNeed.DATA],
        confidence=0.92,
    )

    plan = ChatSupervisor().build_plan(intent)

    assert plan.route == "data_analysis"
    assert plan.required_agents == ["data_analysis"]
    assert plan.requires_metric_query is True


def test_supervisor_routes_data_based_advice_to_two_step_plan() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_DATA,
        intent=ChatIntent.DATA_BASED_ADVICE,
        output_needs=[OutputNeed.DATA, OutputNeed.ADVICE],
        confidence=0.91,
    )

    plan = ChatSupervisor().build_plan(intent)

    assert plan.route == "data_based_advice"
    assert plan.required_agents == ["data_analysis", "business_advice"]
    assert plan.requires_metric_query is True


def test_supervisor_routes_domain_advice_without_metric_query() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_KNOWLEDGE,
        intent=ChatIntent.DOMAIN_ADVICE,
        output_needs=[OutputNeed.ADVICE],
        confidence=0.9,
    )

    plan = ChatSupervisor().build_plan(intent)

    assert plan.route == "domain_advice"
    assert plan.required_agents == ["business_advice"]
    assert plan.requires_metric_query is False


def test_supervisor_routes_risk_to_safety() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_KNOWLEDGE,
        intent=ChatIntent.DOMAIN_ADVICE,
        output_needs=[OutputNeed.ADVICE],
        confidence=0.99,
        risk_level=RiskLevel.URGENT,
    )

    plan = ChatSupervisor().build_plan(intent)

    assert plan.route == "safety"
    assert plan.required_agents == []
    assert plan.requires_metric_query is False
    assert plan.safety_message is not None


def test_supervisor_routes_clarification_without_metric_query() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_DATA,
        intent=ChatIntent.METRIC_ANALYSIS,
        confidence=0.52,
        needs_clarification=True,
        clarification_question="你想分析体重、BMI、体脂率还是腰围？",
    )

    plan = ChatSupervisor().build_plan(intent)

    assert plan.route == "clarification"
    assert plan.required_agents == []
    assert plan.requires_metric_query is False
    assert plan.clarification_question == intent.clarification_question


def test_supervisor_plan_rejects_route_intent_mismatch() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_KNOWLEDGE,
        intent=ChatIntent.DOMAIN_ADVICE,
        confidence=0.9,
    )

    with pytest.raises(ValidationError):
        SupervisorPlan(
            route="data_analysis",
            intent_result=intent,
            required_agents=["data_analysis"],
            requires_metric_query=True,
        )
