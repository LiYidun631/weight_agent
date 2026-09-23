from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from weight_agent.chat.models import ComparisonMode
from weight_agent.domain.time.models import ResolvedTimeRange, TimeRange
from weight_agent.domain.time.resolver import TimeRangeResolver

NOW = datetime(2026, 9, 22, 15, 30, tzinfo=UTC)


def test_resolves_today_in_user_timezone() -> None:
    result = TimeRangeResolver().resolve("今天", timezone="Asia/Shanghai", now=NOW)

    assert result.confidence == 0.98
    assert result.current is not None
    assert result.current.start_at.isoformat() == "2026-09-22T00:00:00+08:00"
    assert result.current.end_at.isoformat() == "2026-09-23T00:00:00+08:00"


def test_resolves_recent_days_as_rolling_period() -> None:
    result = TimeRangeResolver().resolve("最近 7 天", timezone="UTC", now=NOW)

    assert result.current is not None
    assert result.current.start_at == datetime(2026, 9, 15, 15, 30, 1, tzinfo=UTC)
    assert result.current.end_at == datetime(2026, 9, 22, 15, 30, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("最近一次", 1), ("最近的", None), ("最近两次", 2), ("最近三次", 3), ("最近10次", 10)],
)
def test_resolves_latest_count_without_faking_a_date_range(
    expression: str,
    expected: int | None,
) -> None:
    result = TimeRangeResolver().resolve(expression, timezone="UTC", now=NOW)

    if expected is None:
        assert result.needs_clarification is True
    else:
        assert result.latest_count == expected
        assert result.current is None
        assert result.resolution_kind == "latest_count"


def test_resolves_previous_period_comparison() -> None:
    result = TimeRangeResolver().resolve(
        "最近30天",
        timezone="UTC",
        now=NOW,
        comparison_mode=ComparisonMode.PREVIOUS_PERIOD,
    )

    assert result.baseline is not None
    assert result.comparison_mode is ComparisonMode.PREVIOUS_PERIOD
    assert result.baseline.end_at == result.current.start_at


def test_resolves_natural_month_and_explicit_range() -> None:
    resolver = TimeRangeResolver()
    month = resolver.resolve("上个月", timezone="Asia/Shanghai", now=NOW)
    explicit = resolver.resolve("2026-09-01 到 2026-09-10", timezone="UTC", now=NOW)

    assert month.current is not None
    assert month.current.start_at.isoformat() == "2026-08-01T00:00:00+08:00"
    assert month.current.end_at.isoformat() == "2026-09-01T00:00:00+08:00"
    assert explicit.current is not None
    assert explicit.current.end_at == datetime(2026, 9, 11, tzinfo=UTC)


def test_unknown_expression_returns_clarification() -> None:
    result = TimeRangeResolver().resolve("春节前后", timezone="UTC", now=NOW)

    assert result.needs_clarification is True
    assert result.current is None
    assert result.confidence < 0.55
    assert result.clarification_question is not None


def test_reversed_explicit_range_returns_clarification() -> None:
    result = TimeRangeResolver().resolve(
        "2026年9月30日到2026年9月1日", timezone="UTC", now=NOW
    )

    assert result.needs_clarification is True
    assert result.current is None
    assert result.reason_codes == ["explicit_range_start_not_before_end"]
    assert result.clarification_question is not None


def test_missing_expression_uses_default_recent_period() -> None:
    result = TimeRangeResolver().resolve(None, timezone="UTC", now=NOW)

    assert result.is_default is True
    assert result.current is not None
    assert result.reason_codes == ["default_recent_period"]


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        TimeRangeResolver().resolve("今天", timezone="Mars/Olympus", now=NOW)


def test_time_models_reject_naive_or_invalid_ranges() -> None:
    with pytest.raises(ValidationError):
        TimeRange(
            start_at=datetime(2026, 1, 1),
            end_at=datetime(2026, 1, 2),
            timezone="UTC",
        )
    with pytest.raises(ValidationError):
        ResolvedTimeRange(
            comparison_mode=ComparisonMode.PREVIOUS_PERIOD,
            confidence=0.9,
        )
