"""内存版指标仓库：用于测试与本地开发，生产环境需替换为数据库适配器。"""

from datetime import UTC, datetime

from weight_agent.domain.metrics.models import (
    MetricObservation,
    MetricQuery,
    MetricResult,
    MetricType,
)


class InMemoryMetricRepository:
    """基于字典的内存指标仓库。"""

    def __init__(self, observations: dict[str, list[MetricObservation]] | None = None) -> None:
        # 观测数据按用户 ID 分组存储
        self._observations = observations or {}

    async def list_available_metrics(self, user_id: str) -> set[MetricType]:
        """返回该用户所有观测中出现过的指标集合。"""
        return {item.metric for item in self._observations.get(user_id, [])}

    async def get_observations(self, query: MetricQuery) -> MetricResult:
        """按指标集合与时间区间过滤观测记录，并按测量时间升序排列。"""
        rows = [
            item
            for item in self._observations.get(query.user_id, [])
            if item.metric in query.metrics and query.start_at <= item.measured_at <= query.end_at
        ]
        rows.sort(key=lambda item: item.measured_at)
        return MetricResult(
            observations=rows,
            available_metrics={item.metric for item in rows},
            queried_at=datetime.now(UTC),
        )

    async def get_latest_observations(
        self,
        user_id: str,
        metrics: set[MetricType],
        before: datetime | None = None,
    ) -> list[MetricObservation]:
        """返回每个指标在截止时间之前最新的一条观测。"""
        # 未指定截止时间时视为“无限远”，即不过滤
        cutoff = before or datetime.max.replace(tzinfo=UTC)
        latest: dict[MetricType, MetricObservation] = {}
        for item in self._observations.get(user_id, []):
            if (
                item.metric in metrics
                and item.measured_at <= cutoff
                and (
                    # 每个指标只保留时间最新的一条
                    item.metric not in latest or item.measured_at > latest[item.metric].measured_at
                )
            ):
                latest[item.metric] = item
        return list(latest.values())
