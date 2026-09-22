"""健康检查路由：供探活与监控使用。"""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from weight_agent import __version__

router = APIRouter()


class HealthResponse(BaseModel):
    """健康检查响应结构。"""

    status: Literal["ok"]  # 服务状态，固定为 ok
    service: str  # 服务名称
    version: str  # 服务版本号
    environment: str  # 当前运行环境


@router.get("/health", response_model=HealthResponse, summary="服务健康检查")
async def health_check(request: Request) -> HealthResponse:
    """返回服务名称、版本与运行环境，用于存活探测。"""
    settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
    )
