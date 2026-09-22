"""时间范围解析使用的结构化领域模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from weight_agent.chat.models import ComparisonMode


class TimeRange(BaseModel):
    """带时区的半开时间区间 ``[start_at, end_at)``。"""

    model_config = ConfigDict(extra="forbid")

    start_at: datetime
    end_at: datetime
    timezone: str = Field(min_length=1, max_length=64)
    source_expression: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_range(self) -> "TimeRange":
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("time range datetimes must be timezone-aware")
        if self.start_at >= self.end_at:
            raise ValueError("start_at must be before end_at")
        return self


class ResolvedTimeRange(BaseModel):
    """规则解析后的当前周期、可选基准周期和澄清状态。"""

    model_config = ConfigDict(extra="forbid")

    current: TimeRange | None = None
    baseline: TimeRange | None = None
    comparison_mode: ComparisonMode = ComparisonMode.NONE
    latest_count: int | None = Field(default=None, ge=1, le=1000)
    resolution_kind: str = Field(default="period", max_length=32)
    confidence: float = Field(ge=0, le=1)
    is_default: bool = False
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=256)
    reason_codes: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_clarification(self) -> "ResolvedTimeRange":
        if self.needs_clarification and not self.clarification_question:
            raise ValueError("clarification_question is required when clarification is needed")
        if self.comparison_mode is not ComparisonMode.NONE and self.baseline is None:
            raise ValueError("baseline is required when comparison is enabled")
        return self
