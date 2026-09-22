"""应用入口：组装 FastAPI 应用与各业务工作流。"""

import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from weight_agent import __version__
from weight_agent.api.router import api_router
from weight_agent.chat.intent import HybridIntentClassifier, RuleBasedIntentClassifier
from weight_agent.chat.memory import InMemoryConversationMemory
from weight_agent.chat.workflow import ChatWorkflow
from weight_agent.core.config import Settings, get_settings
from weight_agent.report.qwen import QwenReportAnalyzer
from weight_agent.report.workflow import (
    KeyEvidenceProvider,
    ReportAnalyzer,
    ReportWorkflow,
    UnavailableReportAnalyzer,
)

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理：启动与关闭时输出日志。"""
    settings: Settings = app.state.settings
    logger.info("Starting %s in %s", settings.app_name, settings.environment)
    yield
    logger.info("Stopping %s", settings.app_name)


def create_app(
    settings: Settings | None = None,
    report_analyzer: ReportAnalyzer | None = None,
    key_evidence_provider: KeyEvidenceProvider | None = None,
) -> FastAPI:
    """应用工厂：创建并配置 FastAPI 实例（支持注入配置与分析器便于测试）。"""
    resolved_settings = settings or get_settings()
    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        debug=resolved_settings.debug,
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_response(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 非有限数只在错误详情中转为文本，避免非法输入使 422 序列化成 500。
        errors = jsonable_encoder(
            exc.errors(),
            custom_encoder={float: lambda value: value if math.isfinite(value) else str(value)},
        )
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def log_request_duration(request, call_next):
        started_at = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            status_code = response.status_code if response is not None else 500
            message = (
                "http_request_completed "
                f"method={request.method} path={request.url.path} "
                f"status_code={status_code} duration_ms={duration_ms:.2f}"
            )
            # uvicorn.error 通常会输出到 Uvicorn 窗口；print 作为 IDE/reload
            # 场景下的可靠兜底，确保开发者能直接看到耗时。
            access_logger.info(message)
            print(f"[REQUEST_DURATION] {message}", flush=True)
            if response is not None:
                response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"

    # 将配置与工作流挂到应用状态上，供路由处理器使用
    app.state.settings = resolved_settings
    rule_classifier = RuleBasedIntentClassifier()
    app.state.chat_workflow = ChatWorkflow(
        intent_classifier=HybridIntentClassifier(
            primary=rule_classifier,
            fallback=rule_classifier,
            min_confidence=resolved_settings.chat_intent_min_confidence,
            fallback_confidence=resolved_settings.chat_intent_fallback_confidence,
            rule_confidence=resolved_settings.chat_intent_rule_confidence,
        )
    )
    app.state.chat_memory = InMemoryConversationMemory(
        ttl=timedelta(hours=resolved_settings.chat_memory_ttl_hours),
        max_turns=resolved_settings.chat_memory_max_turns,
    )
    resolved_report_analyzer = report_analyzer or _build_report_analyzer(resolved_settings)
    app.state.report_workflow = ReportWorkflow(
        analyzer=resolved_report_analyzer,
        timeout_seconds=resolved_settings.report_model_timeout_seconds,
        key_evidence_provider=key_evidence_provider,
    )
    app.include_router(api_router, prefix=resolved_settings.api_v1_prefix)
    return app


def _build_report_analyzer(settings: Settings) -> ReportAnalyzer:
    """按配置构建报告分析器：未配置密钥时返回占位实现。"""
    if settings.dashscope_api_key is None:
        return UnavailableReportAnalyzer()
    return QwenReportAnalyzer(
        api_key=settings.dashscope_api_key.get_secret_value(),
        base_url=settings.report_model_base_url,
        model=settings.report_model,
        request_timeout_seconds=settings.report_model_timeout_seconds,
        temperature=settings.report_model_temperature,
        max_completion_tokens=settings.report_model_max_completion_tokens,
        enable_thinking=settings.report_model_enable_thinking,
    )


# 模块级应用实例，供 uvicorn 等 ASGI 服务器直接加载
app = create_app()
