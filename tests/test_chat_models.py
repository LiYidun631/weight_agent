from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from weight_agent.chat.models import (
    AdviceCategory,
    AdviceFact,
    AdviceFacts,
    AnalysisResult,
    ChatDomain,
    ChatIntent,
    ConversationContext,
    ConversationTurn,
    IntentResult,
    OutputNeed,
    QueryEntities,
    RiskLevel,
    SupervisorPlan,
)
from weight_agent.domain.metrics.models import MetricType


def test_intent_result_requires_clarification_question() -> None:
    with pytest.raises(ValidationError):
        IntentResult(
            domain=ChatDomain.HEALTH_DATA,
            intent=ChatIntent.METRIC_ANALYSIS,
            confidence=0.6,
            needs_clarification=True,
        )


def test_supervisor_plan_enforces_route_agents() -> None:
    intent = IntentResult(
        domain=ChatDomain.HEALTH_DATA,
        intent=ChatIntent.DATA_BASED_ADVICE,
        output_needs=[OutputNeed.DATA, OutputNeed.ADVICE],
        confidence=0.95,
    )
    with pytest.raises(ValidationError):
        SupervisorPlan(
            route="data_based_advice",
            intent_result=intent,
            required_agents=["business_advice"],
            requires_metric_query=True,
        )


def test_core_models_preserve_structured_context() -> None:
    now = datetime.now(UTC)
    analysis = AnalysisResult(
        metric=MetricType.WEIGHT,
        observation_count=4,
        first_value=Decimal("80.0"),
        last_value=Decimal("78.8"),
        absolute_change=Decimal("-1.2"),
        trend="down",
        period_start=now,
        period_end=now,
    )
    context = ConversationContext(
        conversation_id="conv-1",
        user_id="user-1",
        recent_turns=[
            ConversationTurn(role="user", content="分析最近一个月体重", created_at=now),
            ConversationTurn(role="assistant", content="体重呈下降趋势", created_at=now),
        ],
        confirmed_entities=QueryEntities(
            metrics=[MetricType.WEIGHT],
            advice_categories=[AdviceCategory.DIET],
        ),
        last_intent=ChatIntent.METRIC_ANALYSIS,
        last_analysis=analysis,
        expires_at=now,
    )
    assert context.last_analysis is not None
    assert context.last_analysis.absolute_change == Decimal("-1.2")
    assert context.confirmed_entities is not None


def test_advice_facts_are_traceable() -> None:
    facts = AdviceFacts(
        categories=[AdviceCategory.DIET],
        facts=[
            AdviceFact(
                fact_id="trend.weight.down",
                statement="最近周期体重呈下降趋势",
                evidence=["analysis.weight.trend"],
                risk_level=RiskLevel.NONE,
            )
        ],
    )
    assert facts.facts[0].evidence == ["analysis.weight.trend"]
