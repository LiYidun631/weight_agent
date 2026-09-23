"""规则优先的自然语言时间范围解析器。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from weight_agent.chat.models import ComparisonMode
from weight_agent.domain.time.models import ResolvedTimeRange, TimeRange


def load_timezone(name: str) -> ZoneInfo | timezone:
    """加载 IANA 时区；常用默认时区在 tzdata 不可用时提供固定偏移兜底。"""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        fallback_offsets = {
            "UTC": UTC,
            "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
        }
        if name in fallback_offsets:
            return fallback_offsets[name]
        raise ValueError(f"unknown timezone: {name}") from exc


class TimeRangeResolver:
    """解析常见中文时间表达，最终日期计算完全由确定性代码完成。"""

    _recent_pattern = re.compile(
        r"最近\s*(\d+|一|两|二|三|七|十四|三十)\s*(?:个)?(天|日|周|星期|月)"
    )
    _latest_count_pattern = re.compile(
        r"最近\s*(一次|一条|一笔|两次|两条|二次|三次|三条|\d+\s*次|\d+\s*条)"
    )
    _date_range_pattern = re.compile(
        r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?"
        r"\s*(?:到|至|-|~)\s*"
        r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?"
    )

    def resolve(
        self,
        expression: str | None,
        *,
        timezone: str,
        now: datetime | None = None,
        comparison_mode: ComparisonMode = ComparisonMode.NONE,
        default_days: int = 30,
    ) -> ResolvedTimeRange:
        """解析时间表达；无法可靠解析时返回澄清状态。"""
        zone = load_timezone(timezone)
        current_now = self._coerce_now(now, zone)
        text = (expression or "").strip()

        if not text:
            current = self._rolling_range(current_now, timedelta(days=default_days), zone, None)
            return ResolvedTimeRange(
                current=current,
                confidence=0.7,
                is_default=True,
                reason_codes=["default_recent_period"],
            )

        try:
            parsed = self._parse_explicit_date_range(text, zone)
        except ValueError:
            return ResolvedTimeRange(
                confidence=0.5,
                needs_clarification=True,
                clarification_question="起止日期的顺序似乎反了，请确认开始日期早于结束日期。",
                reason_codes=["explicit_range_start_not_before_end"],
            )
        if parsed:
            return self._with_comparison(parsed, comparison_mode, text, zone)

        latest_match = self._latest_count_pattern.search(text)
        if latest_match:
            count = self._parse_latest_count(latest_match.group(1))
            return ResolvedTimeRange(
                confidence=0.97,
                latest_count=count,
                resolution_kind="latest_count",
                reason_codes=["rule_latest_count_match"],
            )

        recent_match = self._recent_pattern.search(text)
        if recent_match:
            amount_text = recent_match.group(1)
            chinese_amounts = {
                "一": 1,
                "两": 2,
                "二": 2,
                "三": 3,
                "七": 7,
                "十四": 14,
                "三十": 30,
            }
            amount = chinese_amounts.get(amount_text)
            if amount is None:
                amount = int(amount_text)
            unit = recent_match.group(2)
            duration = {
                "天": timedelta(days=amount),
                "日": timedelta(days=amount),
                "周": timedelta(weeks=amount),
                "星期": timedelta(weeks=amount),
                "月": timedelta(days=amount * 30),
            }[unit]
            current = self._rolling_range(current_now, duration, zone, text)
            return self._with_comparison(current, comparison_mode, text, zone)

        normalized = re.sub(r"[\s，。！？!?]", "", text)
        if normalized in {"今天", "今日"}:
            current = self._calendar_day(current_now, 0, zone, text)
        elif normalized == "昨天":
            current = self._calendar_day(current_now, -1, zone, text)
        elif normalized in {"本周", "这周", "这星期"}:
            current = self._week_range(current_now, 0, zone, text)
        elif normalized in {"上周", "上星期"}:
            current = self._week_range(current_now, -1, zone, text)
        elif normalized in {"本月", "这个月"}:
            current = self._month_range(current_now, 0, zone, text)
        elif normalized in {"上个月", "上月"}:
            current = self._month_range(current_now, -1, zone, text)
        elif normalized in {"今年", "本年"}:
            current = self._year_range(current_now, 0, zone, text)
        elif normalized in {"去年", "上一年"}:
            current = self._year_range(current_now, -1, zone, text)
        else:
            return ResolvedTimeRange(
                confidence=0.2,
                needs_clarification=True,
                clarification_question="你想看最近 30 天，还是指定一个起止日期？",
                reason_codes=["unrecognized_time_expression"],
            )

        return self._with_comparison(current, comparison_mode, text, zone)

    @staticmethod
    def _coerce_now(now: datetime | None, zone: ZoneInfo) -> datetime:
        value = now or datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(zone)

    @staticmethod
    def _parse_latest_count(value: str) -> int:
        normalized = re.sub(r"\s*(次|条|笔)", "", value)
        chinese_counts = {"一次": 1, "一": 1, "两": 2, "二": 2, "三": 3}
        if normalized in chinese_counts:
            return chinese_counts[normalized]
        return int(normalized)

    @staticmethod
    def _rolling_range(
        end: datetime,
        duration: timedelta,
        zone: ZoneInfo | timezone,
        expression: str | None,
    ) -> TimeRange:
        end_boundary = end.replace(microsecond=0) + timedelta(seconds=1)
        return TimeRange(
            start_at=(end_boundary - duration).astimezone(zone),
            end_at=end_boundary.astimezone(zone),
            timezone=str(zone),
            source_expression=expression,
        )

    @staticmethod
    def _calendar_day(
        now: datetime,
        offset: int,
        zone: ZoneInfo | timezone,
        expression: str,
    ) -> TimeRange:
        target = now.date() + timedelta(days=offset)
        start = datetime.combine(target, time.min, tzinfo=zone)
        return TimeRange(
            start_at=start,
            end_at=start + timedelta(days=1),
            timezone=str(zone),
            source_expression=expression,
        )

    @staticmethod
    def _week_range(
        now: datetime,
        offset: int,
        zone: ZoneInfo | timezone,
        expression: str,
    ) -> TimeRange:
        monday = now.date() - timedelta(days=now.weekday()) + timedelta(weeks=offset)
        start = datetime.combine(monday, time.min, tzinfo=zone)
        return TimeRange(
            start_at=start,
            end_at=start + timedelta(days=7),
            timezone=str(zone),
            source_expression=expression,
        )

    @staticmethod
    def _month_range(
        now: datetime,
        offset: int,
        zone: ZoneInfo | timezone,
        expression: str,
    ) -> TimeRange:
        month_index = now.year * 12 + now.month - 1 + offset
        year, month_index = divmod(month_index, 12)
        month = month_index + 1
        start = datetime(year, month, 1, tzinfo=zone)
        next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=zone)
        return TimeRange(
            start_at=start,
            end_at=next_month,
            timezone=str(zone),
            source_expression=expression,
        )

    @staticmethod
    def _year_range(
        now: datetime,
        offset: int,
        zone: ZoneInfo | timezone,
        expression: str,
    ) -> TimeRange:
        year = now.year + offset
        start = datetime(year, 1, 1, tzinfo=zone)
        return TimeRange(
            start_at=start,
            end_at=datetime(year + 1, 1, 1, tzinfo=zone),
            timezone=str(zone),
            source_expression=expression,
        )

    def _parse_explicit_date_range(
        self,
        text: str,
        zone: ZoneInfo | timezone,
    ) -> TimeRange | None:
        match = self._date_range_pattern.search(text)
        if not match:
            return None
        start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        end = date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
        if start >= end:
            raise ValueError("explicit time range start must be before end")
        return TimeRange(
            start_at=datetime.combine(start, time.min, tzinfo=zone),
            end_at=datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone),
            timezone=str(zone),
            source_expression=text,
        )

    def _with_comparison(
        self,
        current: TimeRange,
        comparison_mode: ComparisonMode,
        expression: str,
        zone: ZoneInfo | timezone,
    ) -> ResolvedTimeRange:
        baseline = None
        if comparison_mode is ComparisonMode.PREVIOUS_PERIOD:
            duration = current.end_at - current.start_at
            baseline = TimeRange(
                start_at=(current.start_at - duration).astimezone(zone),
                end_at=current.start_at.astimezone(zone),
                timezone=str(zone),
                source_expression=f"{expression}的上一周期",
            )
        return ResolvedTimeRange(
            current=current,
            baseline=baseline,
            comparison_mode=comparison_mode,
            confidence=0.98,
            reason_codes=["rule_time_match"],
        )
