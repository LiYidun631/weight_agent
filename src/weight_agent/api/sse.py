"""SSE（Server-Sent Events）事件编码工具。"""

import json
from collections.abc import AsyncIterator
from typing import Any

from weight_agent.chat.workflow import event_timestamp


async def encode_events(
    events: AsyncIterator[tuple[str, dict[str, Any]]],
) -> AsyncIterator[str]:
    """将工作流事件流编码为符合 SSE 规范的文本流。

    每条事件自动附带自增序号、请求 ID 与 UTC 时间戳，
    与原始负载合并后序列化为 JSON 输出。
    """
    sequence = 0  # 事件自增序号
    request_id: str | None = None  # 从首条事件中提取的请求 ID
    async for event_name, payload in events:
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
