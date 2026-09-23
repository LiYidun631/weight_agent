"""SSE（Server-Sent Events）事件编码工具。"""

import json
from collections.abc import AsyncIterator
from typing import Any

from weight_agent.chat.workflow import event_timestamp


async def encode_events(
    events: AsyncIterator[tuple[str, dict[str, Any]]],
    *,
    include_metadata: bool = True,
) -> AsyncIterator[str]:
    """将工作流事件流编码为符合 SSE 规范的文本流。

    调试模式会在原始负载上附带自增序号、请求 ID 与 UTC 时间戳。
    公共模式只输出客户端需要的最小字段，避免把工作流内部信息暴露给前端。
    """
    sequence = 0  # 事件自增序号
    request_id: str | None = None  # 从首条事件中提取的请求 ID
    async for event_name, payload in events:
        if not include_metadata:
            # 公共协议只保留文本流和生命周期事件；内部 progress/data 事件
            # 即使上游误产出，也不会泄露到客户端。
            if event_name not in {"start", "delta", "error", "done"}:
                continue
            if event_name == "start":
                data = {"conversation_id": payload.get("conversation_id")}
            elif event_name == "delta":
                data = {"content": payload.get("content", "")}
            elif event_name == "error":
                # 错误事件只暴露稳定的错误码与可重试标记，不泄露内部细节
                data = {
                    "code": payload.get("code", "INTERNAL_ERROR"),
                    "message": payload.get("message", "服务暂时不可用，请稍后重试"),
                    "retryable": bool(payload.get("retryable", False)),
                }
            else:
                data = {
                    "status": payload.get("status", "completed"),
                    "conversation_id": payload.get("conversation_id"),
                }
            yield f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            continue

        sequence += 1
        # 请求 ID 以首条事件为准，缺失时使用占位值
        request_id = request_id or payload.get("request_id", "unknown")
        data = {
            "request_id": request_id,
            "sequence": sequence,
            "timestamp": event_timestamp(),
            **payload,
        }
        # 按 SSE 规范输出：event 行 + data 行 + 空行分隔
        yield f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
