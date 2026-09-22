"""Chat 短期会话记忆的抽象与单进程实现。"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from weight_agent.chat.models import ConversationContext, ConversationTurn


class ConversationMemory(Protocol):
    """会话记忆的存储契约。

    实现必须以 user_id 和 conversation_id 的组合键隔离会话。
    """

    async def load(self, user_id: str, conversation_id: str) -> ConversationContext | None:
        """读取未过期的会话上下文。"""

    async def append_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn: ConversationTurn,
    ) -> None:
        """追加一条最终消息并续期会话。"""

    async def update_context(
        self,
        user_id: str,
        conversation_id: str,
        context: ConversationContext,
    ) -> None:
        """替换会话上下文并续期会话。"""

    async def delete(self, user_id: str, conversation_id: str) -> None:
        """删除会话。"""


class InMemoryConversationMemory:
    """用于开发和测试的带 TTL 会话记忆。

    该实现不用于多实例生产部署；生产环境可用 Redis 实现同一协议。
    """

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(hours=24),
        max_turns: int = 10,
        now: callable | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.ttl = ttl
        self.max_turns = max_turns
        self._now = now or (lambda: datetime.now(UTC))
        self._store: MutableMapping[tuple[str, str], ConversationContext] = {}

    async def load(self, user_id: str, conversation_id: str) -> ConversationContext | None:
        key = (user_id, conversation_id)
        context = self._store.get(key)
        if context is None:
            return None
        if self._is_expired(context):
            self._store.pop(key, None)
            return None
        return context.model_copy(deep=True)

    async def append_turn(
        self,
        user_id: str,
        conversation_id: str,
        turn: ConversationTurn,
    ) -> None:
        key = (user_id, conversation_id)
        context = self._store.get(key)
        if context is None or self._is_expired(context):
            context = ConversationContext(
                conversation_id=conversation_id,
                user_id=user_id,
                expires_at=self._expires_at(),
            )
        self._assert_owner(context, user_id, conversation_id)
        context.recent_turns.append(turn)
        context.recent_turns = context.recent_turns[-self.max_turns :]
        context.expires_at = self._expires_at()
        self._store[key] = context

    async def update_context(
        self,
        user_id: str,
        conversation_id: str,
        context: ConversationContext,
    ) -> None:
        self._assert_owner(context, user_id, conversation_id)
        if self._is_expired(context):
            raise ValueError("cannot update an expired conversation")
        copied = context.model_copy(deep=True)
        copied.recent_turns = copied.recent_turns[-self.max_turns :]
        copied.expires_at = self._expires_at()
        self._store[(user_id, conversation_id)] = copied

    async def delete(self, user_id: str, conversation_id: str) -> None:
        self._store.pop((user_id, conversation_id), None)

    def _expires_at(self) -> datetime:
        return self._now() + self.ttl

    def _is_expired(self, context: ConversationContext) -> bool:
        expires_at = context.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= self._now()

    @staticmethod
    def _assert_owner(
        context: ConversationContext,
        user_id: str,
        conversation_id: str,
    ) -> None:
        if context.user_id != user_id or context.conversation_id != conversation_id:
            raise ValueError("conversation does not belong to user")
