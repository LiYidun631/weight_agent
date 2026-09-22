"""API 路由汇总：将各业务子路由挂载到统一入口。"""

from fastapi import APIRouter

from weight_agent.api.routes.chat import router as chat_router
from weight_agent.api.routes.health import router as health_router
from weight_agent.api.routes.report import router as report_router

# 顶层 API 路由入口，最终由 main.create_app 挂载到应用
api_router = APIRouter()
api_router.include_router(health_router, tags=["system"])  # 健康检查
api_router.include_router(chat_router, tags=["chat"])  # 流式 Chat 对话
api_router.include_router(report_router, tags=["report"])  # 身体成分报告
