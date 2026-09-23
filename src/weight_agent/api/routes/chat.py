"""Chat 对话路由：提供流式对话接口。"""

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from weight_agent.api.schemas.chat import ChatRequest
from weight_agent.api.sse import encode_events
from weight_agent.chat.workflow import ChatWorkflow

router = APIRouter()


@router.post("/chat/stream", summary="流式 Chat 对话")
async def chat_stream(
    request: Request,
    payload: ChatRequest,
    x_user_id: str | None = Header(default=None, max_length=128),
) -> StreamingResponse:
    """处理流式 Chat 请求，以 SSE 形式持续返回工作流事件。"""
    # 未提供用户 ID 时按匿名用户处理
    user_id = x_user_id or "anonymous"
    # 从应用状态取工作流实例；测试环境缺失时退回默认实例
    workflow = getattr(request.app.state, "chat_workflow", ChatWorkflow())
    events = workflow.run(payload, user_id=user_id)
    settings = getattr(request.app.state, "settings", None)
    include_metadata = bool(getattr(settings, "chat_expose_debug_events", False))
    return StreamingResponse(
        encode_events(events, include_metadata=include_metadata),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",  # 禁用缓存保证实时性
            "Connection": "keep-alive",  # 保持长连接
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲，事件即时下发
        },
    )
