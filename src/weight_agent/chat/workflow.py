"""Chat 工作流：处理对话请求并以事件流形式输出。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from weight_agent.api.schemas.chat import ChatRequest
from weight_agent.chat.executor import RouteExecutor
from weight_agent.chat.intent import (
    ClassifierContext,
    HybridIntentClassifier,
    IntentClassifier,
    RuleBasedIntentClassifier,
)
from weight_agent.chat.node import NodeContext
from weight_agent.chat.supervisor import ChatSupervisor
from weight_agent.core.config import get_settings


class ChatWorkflow:
    """Chat 工作流编排入口。

    当前为稳定实现，为后续接入 LangGraph StateGraph 预留扩展点。
    """

    def __init__(
        self,
        intent_classifier: IntentClassifier | None = None,
        supervisor: ChatSupervisor | None = None,
        route_executor: RouteExecutor | None = None,
    ) -> None:
        """创建 Chat 工作流。

        默认先使用规则分类器作为 Hybrid 的 primary 和 fallback，使工作流具备完整
        意图识别链路；后续接入真实 LLM 时，只需要注入 LlmIntentClassifier 作为
        primary，fallback 仍保留规则分类器。
        """
        rule_classifier = RuleBasedIntentClassifier()
        self._intent_classifier = intent_classifier or HybridIntentClassifier(
            primary=rule_classifier,
            fallback=rule_classifier,
        )
        self._supervisor = supervisor or ChatSupervisor()
        self._route_executor = route_executor or RouteExecutor()
        self._default_timezone = get_settings().chat_default_timezone

    async def run(self, request: ChatRequest, user_id: str) -> AsyncIterator[tuple[str, dict]]:
        """执行 Chat 工作流，依次产出 (事件名, 负载) 事件流。"""
        # 生成请求 ID 与会话 ID（未提供会话 ID 时新建）
        request_id = f"req_{uuid4().hex}"
        conversation_id = request.conversation_id or f"conv_{uuid4().hex}"
        yield "start", {"conversation_id": conversation_id, "request_id": request_id}
        yield "progress", {"stage": "understanding", "message": "正在理解你的问题"}
        intent_result = await self._intent_classifier.classify(
            request.message,
            ClassifierContext(),
        )
        yield (
            "data",
            {
                "type": "intent_result",
                "intent_result": intent_result.model_dump(mode="json"),
            },
        )
        plan = self._supervisor.build_plan(intent_result)
        yield (
            "data",
            {
                "type": "supervisor_plan",
                "supervisor_plan": plan.model_dump(mode="json"),
            },
        )
        execution = await self._route_executor.execute(
            plan,
            NodeContext(
                request_id=request_id,
                conversation_id=conversation_id,
                user_id=user_id,
                timezone=request.timezone or self._default_timezone,
            ),
        )
        yield (
            "data",
            {
                "type": "route_execution",
                "route_execution": execution.model_dump(mode="json"),
            },
        )
        if execution.status == "needs_clarification":
            yield (
                "progress",
                {"stage": "clarification", "message": "需要补充一个关键信息"},
            )
            yield (
                "delta",
                {"content": execution.content},
            )
            yield "done", {"status": "needs_clarification", "conversation_id": conversation_id}
            return

        yield "delta", {"content": execution.content}
        yield "done", {"status": "completed", "conversation_id": conversation_id}


def event_timestamp() -> str:
    """返回当前 UTC 时间的 ISO 格式时间戳，用于 SSE 事件。"""
    return datetime.now(UTC).isoformat()
