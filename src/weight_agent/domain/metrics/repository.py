"""指标仓库抽象：定义指标数据访问接口。"""

from datetime import datetime
from typing import Protocol

from weight_agent.domain.metrics.models import (
    MetricObservation,
    MetricQuery,
    MetricResult,
    MetricType,
)


class MetricRepository(Protocol):
    """指标仓库协议（结构类型），具体实现可以是内存版或数据库适配器。"""

    async def list_available_metrics(self, user_id: str) -> set[MetricType]:
        """列出用户当前有数据的指标集合。"""
        ...

    async def get_observations(self, query: MetricQuery) -> MetricResult:
        """按查询条件获取指标观测记录。"""
        ...

    async def get_latest_observations(
        self,
        user_id: str,
        metrics: set[MetricType],
        before: datetime | None = None,
    ) -> list[MetricObservation]:
        """获取指定指标在截止时间（含）之前的最新观测记录。"""
        ...
