"""Chat 对话接口的请求与响应 Schema。"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClientContext(BaseModel):
    """客户端上下文信息。"""

    locale: str = Field(default="zh-CN", max_length=16)  # 客户端语言环境


class ChatRequest(BaseModel):
    """流式 Chat 请求体。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    conversation_id: str | None = Field(default=None, max_length=128)  # 会话 ID（可为空）
    message: str = Field(min_length=1, max_length=4000)  # 用户输入内容
    timezone: str | None = Field(default=None, max_length=64)  # 用户时区
    client_context: ClientContext = Field(default_factory=ClientContext)  # 客户端上下文

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        """校验消息去空格后不能为空。"""
        if not value:
            raise ValueError("message must not be blank")
        return value


class ChatEvent(BaseModel):
    """流式 Chat 事件结构。"""

    request_id: str  # 请求 ID
    sequence: int = Field(ge=1)  # 事件序号（从 1 开始）
    timestamp: str  # 事件时间戳
    payload: dict[str, Any] = Field(default_factory=dict)  # 事件负载


class ChatError(BaseModel):
    """流式 Chat 错误结构。"""

    code: str  # 错误码
    message: str  # 错误描述
    retryable: bool = False  # 是否可重试


class ChatDone(BaseModel):
    """流式 Chat 完成事件。"""

    status: Literal["completed", "needs_clarification"] = "completed"  # 完成状态
    conversation_id: str  # 会话 ID
