"""Chat 工作流：处理对话请求并以事件流形式输出。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from weight_agent.api.schemas.chat import ChatRequest
from weight_agent.chat.advice import HealthAdviceOutputError
from weight_agent.chat.executor import RouteExecutor
from weight_agent.chat.intent import (
    ClassifierContext,
    HybridIntentClassifier,
    IntentClassificationError,
    IntentClassifier,
    RuleBasedIntentClassifier,
)
from weight_agent.chat.memory import ConversationMemory
from weight_agent.chat.models import ChatIntent, ConversationTurn
from weight_agent.chat.node import NodeContext
from weight_agent.chat.normalization import normalize_chat_text
from weight_agent.chat.supervisor import ChatSupervisor
from weight_agent.core.config import get_settings

logger = logging.getLogger(__name__)


def _map_exception(exc: Exception) -> dict[str, Any]:
    """把内部异常映射为不泄露实现细节的 error 事件负载。"""
    if isinstance(exc, IntentClassificationError):
        return {
            "code": "MODEL_OUTPUT_INVALID",
            "message": "意图识别结果异常，请稍后重试",
            "retryable": True,
        }
    if isinstance(exc, HealthAdviceOutputError):
        return {
            "code": "MODEL_OUTPUT_INVALID",
            "message": "建议生成结果异常，请稍后重试",
            "retryable": True,
        }
    if isinstance(exc, TimeoutError):
        return {
            "code": "DEPENDENCY_TIMEOUT",
            "message": "服务响应超时，请稍后重试",
            "retryable": True,
        }
    if isinstance(exc, ValueError):
        return {
            "code": "INVALID_REQUEST",
            "message": "请求参数无效",
            "retryable": False,
        }
    return {
        "code": "INTERNAL_ERROR",
        "message": "服务暂时不可用，请稍后重试",
        "retryable": False,
    }


class ChatWorkflow:
    """Chat 工作流编排入口。

    当前为稳定实现，为后续接入 LangGraph StateGraph 预留扩展点。
    """

    def __init__(
        self,
        intent_classifier: IntentClassifier | None = None,
        supervisor: ChatSupervisor | None = None,
        route_executor: RouteExecutor | None = None,
        memory: ConversationMemory | None = None,
        expose_debug_events: bool = False,
        stream_chunk_size: int | None = None,
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
        self._memory = memory
        self._expose_debug_events = expose_debug_events
        self._stream_chunk_size = stream_chunk_size or get_settings().chat_stream_chunk_size

    async def run(self, request: ChatRequest, user_id: str) -> AsyncIterator[tuple[str, dict]]:
        """执行 Chat 工作流，依次产出 (事件名, 负载) 事件流。

        主体执行期间抛出的异常统一转换为终止性 error 事件，
        保证客户端总是收到明确的结束信号而不是连接裸断。
        """
        request_id = f"req_{uuid4().hex}"
        conversation_id = request.conversation_id or f"conv_{uuid4().hex}"
        try:
            async for event in self._execute(request, user_id, request_id, conversation_id):
                yield event
        except Exception as exc:
            logger.exception(
                "chat workflow failed request_id=%s conversation_id=%s error_type=%s",
                request_id,
                conversation_id,
                type(exc).__name__,
            )
            yield "error", {
                **_map_exception(exc),
                "error_type": type(exc).__name__,
                "request_id": request_id,
                "conversation_id": conversation_id,
            }

    async def _execute(
        self,
        request: ChatRequest,
        user_id: str,
        request_id: str,
        conversation_id: str,
    ) -> AsyncIterator[tuple[str, dict]]:
        """工作流主体：加载记忆、识别意图、执行路由并流式输出。"""
        conversation = (
            await self._memory.load(user_id, conversation_id)
            if self._memory and request.conversation_id
            else None
        )
        language = normalize_chat_text(
            request.message,
            client_locale=request.client_context.locale,
            default_locale=get_settings().chat_default_locale,
        )
        yield "start", {"conversation_id": conversation_id, "request_id": request_id}
        if self._expose_debug_events:
            yield "progress", {"stage": "understanding", "message": "正在理解你的问题"}
        intent_result = await self._intent_classifier.classify(
            request.message,
            ClassifierContext(
                conversation_summary=conversation.summary if conversation else None,
                recent_turns=conversation.recent_turns if conversation else [],
                confirmed_entities=conversation.confirmed_entities if conversation else None,
                pending_entities=conversation.pending_entities if conversation else None,
                last_intent=conversation.last_intent if conversation else None,
                last_analysis=conversation.last_analysis if conversation else None,
                client_locale=request.client_context.locale,
                detected_language=language.detected_language or language.response_language,
                response_language=language.response_language,
            ),
        )
        if self._expose_debug_events:
            yield (
                "data",
                {
                    "type": "intent_result",
                    "intent_result": intent_result.model_dump(mode="json"),
                },
            )
        plan = self._supervisor.build_plan(intent_result)
        if self._expose_debug_events:
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
                message=request.message,
                locale=request.client_context.locale,
                detected_language=language.detected_language or language.response_language,
                response_language=language.response_language,
                timezone=request.timezone or self._default_timezone,
                conversation_summary=conversation.summary if conversation else None,
                recent_turns=conversation.recent_turns if conversation else [],
            ),
        )
        if self._expose_debug_events:
            yield (
                "data",
                {
                    "type": "route_execution",
                    "route_execution": execution.model_dump(mode="json"),
                },
            )
        if execution.status != "completed":
            stage = execution.status
            progress_message = {
                "needs_clarification": "需要补充一个关键信息",
                "needs_data": "当前缺少生成针对性建议所需的数据",
                "blocked": "当前问题需要更谨慎的处理",
            }.get(stage, "当前请求未能完成")
            if self._expose_debug_events:
                yield "progress", {"stage": stage, "message": progress_message}
            async for event in self._stream_text(
                execution.content, chunk_size=self._stream_chunk_size
            ):
                yield event
            await self._save_memory(
                user_id=user_id,
                conversation_id=conversation_id,
                message=request.message,
                answer=execution.content,
                intent_result=intent_result,
                status=execution.status,
            )
            yield "done", {"status": stage, "conversation_id": conversation_id}
            return

        async for event in self._stream_text(
            execution.content, chunk_size=self._stream_chunk_size
        ):
            yield event
        await self._save_memory(
            user_id=user_id,
            conversation_id=conversation_id,
            message=request.message,
            answer=execution.content,
            intent_result=intent_result,
            status="completed",
        )
        yield "done", {"status": "completed", "conversation_id": conversation_id}

    @staticmethod
    async def _stream_text(
        content: str,
        *,
        chunk_size: int = 12,
    ) -> AsyncIterator[tuple[str, dict]]:
        """按短句或适中的文本片段输出 delta。

        当前规则版没有模型 token 流，因此按标点和长度切分，模拟用户可读的
        流式增长效果。接入原生流式模型后，这里应直接转发模型产生的 chunk。
        """
        buffer = ""
        sentence_marks = frozenset("。！？；\n")
        for character in content:
            buffer += character
            if character in sentence_marks or len(buffer) >= chunk_size:
                yield "delta", {"content": buffer}
                buffer = ""
                # 让事件循环有机会把当前片段刷新给网络层。
                await asyncio.sleep(0)
        if buffer:
            yield "delta", {"content": buffer}
            await asyncio.sleep(0)

    async def _save_memory(
        self,
        *,
        user_id: str,
        conversation_id: str,
        message: str,
        answer: str,
        intent_result,
        status: str,
    ) -> None:
        """保存最终轮次与上下文状态。

        澄清轮次把未确认的槽位写入 pending_entities，供下一轮槽位填充合并；
        其余状态写入已确认实体并清除待定槽位。
        """
        if self._memory is None:
            return
        await self._memory.append_turn(
            user_id,
            conversation_id,
            ConversationTurn(role="user", content=message, created_at=datetime.now(UTC)),
        )
        await self._memory.append_turn(
            user_id,
            conversation_id,
            ConversationTurn(role="assistant", content=answer, created_at=datetime.now(UTC)),
        )
        context = await self._memory.load(user_id, conversation_id)
        if context is None:
            return
        context.last_intent = intent_result.intent
        if status == "needs_clarification":
            context.pending_entities = intent_result.entities.model_copy(deep=True)
        else:
            context.pending_entities = None
            # 只有数据类意图携带显式指标时才更新已确认实体，
            # 问候、建议等轮次不得清空上一轮确认的上下文
            if (
                intent_result.intent
                in {
                    ChatIntent.METRIC_QUERY,
                    ChatIntent.METRIC_ANALYSIS,
                    ChatIntent.DATA_BASED_ADVICE,
                }
                and intent_result.entities.metrics
            ):
                context.confirmed_entities = intent_result.entities.model_copy(deep=True)
        await self._memory.update_context(user_id, conversation_id, context)


def event_timestamp() -> str:
    """返回当前 UTC 时间的 ISO 格式时间戳，用于 SSE 事件。"""
    return datetime.now(UTC).isoformat()

