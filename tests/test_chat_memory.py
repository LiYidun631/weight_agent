from datetime import UTC, datetime, timedelta

import pytest

from weight_agent.chat.memory import InMemoryConversationMemory
from weight_agent.chat.models import ConversationContext, ConversationTurn


def test_memory_isolated_by_user_and_conversation() -> None:
    async def scenario() -> None:
        memory = InMemoryConversationMemory()
        turn = ConversationTurn(
            role="user",
            content="分析我的体重",
            created_at=datetime.now(UTC),
        )
        await memory.append_turn("user-1", "conv-1", turn)

        assert await memory.load("user-1", "conv-1") is not None
        assert await memory.load("user-2", "conv-1") is None
        assert await memory.load("user-1", "conv-2") is None

    import asyncio

    asyncio.run(scenario())


def test_memory_keeps_only_recent_turns() -> None:
    async def scenario() -> None:
        memory = InMemoryConversationMemory(max_turns=3)
        for index in range(5):
            await memory.append_turn(
                "user-1",
                "conv-1",
                ConversationTurn(
                    role="user",
                    content=f"消息 {index}",
                    created_at=datetime.now(UTC),
                ),
            )

        context = await memory.load("user-1", "conv-1")
        assert context is not None
        assert [turn.content for turn in context.recent_turns] == ["消息 2", "消息 3", "消息 4"]

    import asyncio

    asyncio.run(scenario())


def test_memory_expires_and_can_be_deleted() -> None:
    async def scenario() -> None:
        current_time = datetime(2026, 9, 21, 12, tzinfo=UTC)
        memory = InMemoryConversationMemory(
            ttl=timedelta(hours=1),
            now=lambda: current_time,
        )
        await memory.append_turn(
            "user-1",
            "conv-1",
            ConversationTurn(
                role="assistant",
                content="已完成分析",
                created_at=current_time,
            ),
        )
        assert await memory.load("user-1", "conv-1") is not None

        current_time = current_time + timedelta(hours=1, seconds=1)
        assert await memory.load("user-1", "conv-1") is None

        await memory.append_turn(
            "user-1",
            "conv-1",
            ConversationTurn(
                role="user",
                content="新会话",
                created_at=current_time,
            ),
        )
        await memory.delete("user-1", "conv-1")
        assert await memory.load("user-1", "conv-1") is None

    import asyncio

    asyncio.run(scenario())


def test_update_context_rejects_wrong_owner() -> None:
    async def scenario() -> None:
        memory = InMemoryConversationMemory()
        context = ConversationContext(
            conversation_id="conv-1",
            user_id="user-1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        with pytest.raises(ValueError, match="does not belong"):
            await memory.update_context("user-2", "conv-1", context)

    import asyncio

    asyncio.run(scenario())
