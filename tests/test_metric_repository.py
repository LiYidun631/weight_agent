from datetime import UTC, datetime
from decimal import Decimal

import pytest

from weight_agent.domain.metrics.memory import InMemoryMetricRepository
from weight_agent.domain.metrics.models import MetricObservation, MetricQuery, MetricType


@pytest.mark.anyio
async def test_repository_filters_by_user_metric_and_period() -> None:
    inside = MetricObservation(
        metric=MetricType.WEIGHT,
        value=Decimal("72.4"),
        unit="kg",
        measured_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    repository = InMemoryMetricRepository(
        {
            "user-1": [
                inside,
                MetricObservation(
                    metric=MetricType.BMI,
                    value=Decimal("22.1"),
                    unit="kg/m2",
                    measured_at=datetime(2026, 9, 10, tzinfo=UTC),
                ),
            ],
            "user-2": [
                MetricObservation(
                    metric=MetricType.WEIGHT,
                    value=Decimal("80"),
                    unit="kg",
                    measured_at=datetime(2026, 9, 10, tzinfo=UTC),
                )
            ],
        }
    )
    query = MetricQuery(
        user_id="user-1",
        metrics={MetricType.WEIGHT},
        start_at=datetime(2026, 9, 1, tzinfo=UTC),
        end_at=datetime(2026, 9, 30, tzinfo=UTC),
        timezone="Asia/Shanghai",
    )

    result = await repository.get_observations(query)

    assert result.observations == [inside]
    assert result.available_metrics == {MetricType.WEIGHT}
