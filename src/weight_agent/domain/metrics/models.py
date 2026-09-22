"""指标领域模型：定义指标类型与观测/查询/结果数据结构。"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MetricType(StrEnum):
    """支持查询的指标类型枚举。"""

    WEIGHT = "weight"  # 体重
    BMI = "bmi"  # 身体质量指数
    BODY_FAT_RATE = "body_fat_rate"  # 体脂率
    WAIST_CIRCUMFERENCE = "waist_circumference"  # 腰围


class MetricObservation(BaseModel):
    """单条指标观测记录（不可变）。"""

    model_config = ConfigDict(frozen=True)

    metric: MetricType  # 指标类型
    value: Decimal  # 测量值
    unit: str = Field(min_length=1, max_length=32)  # 单位
    measured_at: datetime  # 测量时间
    source: str | None = Field(default=None, max_length=64)  # 数据来源（可选）


class MetricQuery(BaseModel):
    """指标查询条件。"""

    user_id: str = Field(min_length=1, max_length=128)  # 用户标识
    metrics: set[MetricType] = Field(min_length=1)  # 要查询的指标集合（至少一个）
    start_at: datetime  # 查询起始时间
    end_at: datetime  # 查询结束时间
    timezone: str = Field(min_length=1, max_length=64)  # 用户时区


class MetricResult(BaseModel):
    """指标查询结果。"""

    observations: list[MetricObservation]  # 命中的观测记录
    available_metrics: set[MetricType]  # 实际有数据的指标集合
    queried_at: datetime  # 查询执行时间
