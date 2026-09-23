import json

from fastapi.testclient import TestClient

from weight_agent.core.config import Settings
from weight_agent.main import create_app


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_name, data))
    return events


def debug_settings(**overrides) -> Settings:
    return Settings(environment="test", chat_expose_debug_events=True, **overrides)


def test_chat_stream_event_contract() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={
                "conversation_id": "conv-existing",
                "message": "分析最近一个月的体重变化",
                "timezone": "Asia/Shanghai",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"

    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[:5] == ["start", "progress", "data", "data", "data"]
    assert names[-1] == "done"
    assert all(name == "delta" for name in names[5:-1])
    assert [data["sequence"] for _, data in events] == list(range(1, len(events) + 1))
    assert len({data["request_id"] for _, data in events}) == 1
    assert events[0][1]["conversation_id"] == "conv-existing"
    assert events[2][1]["type"] == "intent_result"
    assert events[2][1]["intent_result"]["intent"] == "metric_analysis"
    assert events[2][1]["intent_result"]["domain"] == "health_data"
    assert events[2][1]["intent_result"]["entities"]["metrics"] == ["weight"]
    assert events[3][1]["type"] == "supervisor_plan"
    assert events[3][1]["supervisor_plan"]["route"] == "data_analysis"
    assert events[3][1]["supervisor_plan"]["required_agents"] == ["data_analysis"]
    assert events[3][1]["supervisor_plan"]["requires_metric_query"] is True
    assert events[4][1]["type"] == "route_execution"
    assert events[4][1]["route_execution"]["data"]["executed_nodes"] == ["data_analysis"]
    assert events[4][1]["route_execution"]["status"] == "completed"
    assert events[4][1]["route_execution"]["timezone"] == "Asia/Shanghai"
    assert "".join(data["content"] for name, data in events if name == "delta")
    assert events[-1][1] == {
        **{key: events[-1][1][key] for key in ("request_id", "sequence", "timestamp")},
        "status": "completed",
        "conversation_id": "conv-existing",
    }


def test_chat_stream_returns_clarification_when_intent_is_ambiguous() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"conversation_id": "conv-existing", "message": "帮我看看"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[:5] == ["start", "progress", "data", "data", "data"]
    assert names[5] == "progress"
    assert names[-1] == "done"
    assert all(name == "delta" for name in names[6:-1])
    assert events[2][1]["intent_result"]["needs_clarification"] is True
    assert events[3][1]["supervisor_plan"]["route"] == "clarification"
    assert events[4][1]["route_execution"]["status"] == "needs_clarification"
    content = "".join(data["content"] for name, data in events if name == "delta")
    assert content == events[2][1]["intent_result"]["clarification_question"]
    assert events[-1][1]["status"] == "needs_clarification"


def test_chat_stream_passes_request_timezone_to_route_execution() -> None:
    app = create_app(debug_settings(chat_default_timezone="UTC"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={
                "conversation_id": "conv-existing",
                "message": "查询最近30天体重",
                "timezone": "UTC",
            },
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    route_execution = next(
        data for name, data in events if name == "data" and data["type"] == "route_execution"
    )
    assert route_execution["route_execution"]["timezone"] == "UTC"


def test_chat_stream_persists_short_term_memory() -> None:
    from weight_agent.chat.memory import InMemoryConversationMemory
    from weight_agent.chat.workflow import ChatWorkflow

    memory = InMemoryConversationMemory()
    workflow = ChatWorkflow(memory=memory)
    request = {"conversation_id": "conv-memory", "message": "你好"}

    async def run_once() -> None:
        from weight_agent.api.schemas.chat import ChatRequest

        events = workflow.run(ChatRequest.model_validate(request), user_id="user-memory")
        async for _ in events:
            pass

    import asyncio

    asyncio.run(run_once())
    context = asyncio.run(memory.load("user-memory", "conv-memory"))
    assert context is not None
    assert len(context.recent_turns) == 2
    assert context.last_intent.value == "greeting"


def test_chat_stream_routes_greeting() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"conversation_id": "conv-existing", "message": "你好"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[:5] == ["start", "progress", "data", "data", "data"]
    assert names[-1] == "done"
    assert all(name == "delta" for name in names[5:-1])
    assert events[2][1]["intent_result"]["intent"] == "greeting"
    assert events[2][1]["intent_result"]["domain"] == "general"
    assert events[3][1]["supervisor_plan"]["route"] == "greeting"
    assert events[4][1]["route_execution"]["data"]["executed_nodes"] == ["greeting_reply"]
    content = "".join(data["content"] for name, data in events if name == "delta")
    assert "体重管理" in content


def test_chat_stream_public_mode_returns_text_chunks_only() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"conversation_id": "conv-public", "message": "你好"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events[0][0] == "start"
    assert events[-1][0] == "done"
    assert all(name == "delta" for name, _ in events[1:-1])
    assert set(events[0][1]) == {"conversation_id"}
    assert set(events[-1][1]) == {"status", "conversation_id"}
    assert all(set(data) == {"content"} for name, data in events if name == "delta")
    assert all(len(data["content"]) > 1 for name, data in events if name == "delta")
    assert all(
        key not in data
        for _, data in events
        for key in ("request_id", "sequence", "timestamp", "type", "route_execution")
    )
    content = "".join(data["content"] for name, data in events if name == "delta")
    assert "体重管理" in content
    assert len([data for name, data in events if name == "delta"]) > 1


def test_chat_stream_generates_domain_advice() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"conversation_id": "conv-advice", "message": "减脂期间怎么吃？"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    route_execution = next(
        data for name, data in events if name == "data" and data["type"] == "route_execution"
    )
    assert route_execution["route_execution"]["route"] == "domain_advice"
    assert route_execution["route_execution"]["status"] == "completed"
    assert "稳定饮食结构" in route_execution["route_execution"]["content"]
    assert events[-1][1]["status"] == "completed"


def test_chat_stream_routes_direct_diet_question_to_advice() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={
                "conversation_id": "111",
                "message": "我想问一下，减肥期间吃什么",
                "timezone": "Asia/Shanghai",
                "client_context": {"locale": "zh-CN"},
            },
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    content = "".join(data["content"] for name, data in events if name == "delta")
    assert "稳定饮食结构" in content
    assert events[-1][1]["status"] == "completed"


def test_chat_stream_routes_weight_gain_request_to_advice_without_database() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={
                "conversation_id": "weight-gain",
                "message": "我的体重一直下降，应该怎么增重呢",
                "timezone": "Asia/Shanghai",
            },
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    content = "".join(data["content"] for name, data in events if name == "delta")
    assert events[-1][1]["status"] == "completed"
    assert "已生成指标查询计划" not in content
    assert "增加能量摄入" in content


def test_chat_stream_marks_data_based_advice_as_needing_data() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={
                "conversation_id": "conv-data-advice",
                "message": "根据我的最近体重数据给我饮食建议",
            },
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    route_execution = next(
        data for name, data in events if name == "data" and data["type"] == "route_execution"
    )
    assert route_execution["route_execution"]["route"] == "data_based_advice"
    assert route_execution["route_execution"]["status"] == "needs_data"
    assert events[-1][1]["status"] == "needs_data"


def test_chat_stream_rejects_blank_message() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123"},
            json={"message": "   "},
        )

    assert response.status_code == 422


def test_chat_stream_clarification_round_trip_continues_conversation() -> None:
    import asyncio

    from weight_agent.api.schemas.chat import ChatRequest
    from weight_agent.chat.memory import InMemoryConversationMemory
    from weight_agent.chat.workflow import ChatWorkflow

    memory = InMemoryConversationMemory()
    workflow = ChatWorkflow(memory=memory, expose_debug_events=True)

    async def run_turn(message: str) -> list[tuple[str, dict]]:
        events = []
        async for name, payload in workflow.run(
            ChatRequest(conversation_id="conv-clarify", message=message),
            user_id="user-clarify",
        ):
            events.append((name, payload))
        return events

    first = asyncio.run(run_turn("帮我查一下最近的测量数据"))
    assert first[-1] == (
        "done",
        {"status": "needs_clarification", "conversation_id": "conv-clarify"},
    )

    context = asyncio.run(memory.load("user-clarify", "conv-clarify"))
    assert context is not None
    assert context.last_intent.value == "metric_query"
    assert context.pending_entities is not None
    assert len(context.recent_turns) == 2

    second = asyncio.run(run_turn("体重"))
    assert second[-1] == ("done", {"status": "completed", "conversation_id": "conv-clarify"})
    intent_event = next(
        data for name, data in second if name == "data" and data["type"] == "intent_result"
    )
    assert intent_event["intent_result"]["intent"] == "metric_query"
    assert intent_event["intent_result"]["entities"]["metrics"] == ["weight"]
    assert intent_event["intent_result"]["needs_clarification"] is False

    context = asyncio.run(memory.load("user-clarify", "conv-clarify"))
    assert context is not None
    assert context.pending_entities is None


def _memory_workflow() -> tuple[object, object]:
    import asyncio

    from weight_agent.api.schemas.chat import ChatRequest
    from weight_agent.chat.memory import InMemoryConversationMemory
    from weight_agent.chat.workflow import ChatWorkflow

    memory = InMemoryConversationMemory()
    workflow = ChatWorkflow(memory=memory, expose_debug_events=True)

    def run_turn(message: str) -> list[tuple[str, dict]]:
        async def collect() -> list[tuple[str, dict]]:
            events = []
            async for name, payload in workflow.run(
                ChatRequest(conversation_id="conv-entities", message=message),
                user_id="user-entities",
            ):
                events.append((name, payload))
            return events

        return asyncio.run(collect())

    return run_turn, memory


def _intent_event(events: list[tuple[str, dict]]) -> dict:
    return next(
        data for name, data in events if name == "data" and data["type"] == "intent_result"
    )


def test_chat_stream_multi_turn_doc_example_flows_to_data_based_advice() -> None:
    run_turn, _ = _memory_workflow()

    first = run_turn("分析我最近一个月的体重")
    assert first[-1][1]["status"] == "completed"

    second = run_turn("那饮食上怎么调整？")
    intent = _intent_event(second)["intent_result"]
    assert intent["intent"] == "data_based_advice"
    assert intent["entities"]["metrics"] == ["weight"]
    assert intent["entities"]["time_expression"] == "最近一个月"
    assert second[-1][1]["status"] == "needs_data"


def test_chat_stream_greeting_does_not_clobber_confirmed_entities() -> None:
    import asyncio

    run_turn, memory = _memory_workflow()

    assert run_turn("分析我最近一个月的体重")[-1][1]["status"] == "completed"
    assert run_turn("你好")[-1][1]["status"] == "completed"

    context = asyncio.run(memory.load("user-entities", "conv-entities"))
    assert context is not None
    assert context.confirmed_entities is not None
    assert [m.value for m in context.confirmed_entities.metrics] == ["weight"]
    assert context.confirmed_entities.time_expression == "最近一个月"

    third = run_turn("那饮食上怎么调整？")
    intent = _intent_event(third)["intent_result"]
    assert intent["intent"] == "data_based_advice"
    assert intent["entities"]["metrics"] == ["weight"]
    assert third[-1][1]["status"] == "needs_data"


def test_chat_stream_rejects_unknown_timezone_before_stream() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"message": "今天体重多少", "timezone": "Mars/Olympus"},
        )

    assert response.status_code == 422
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_chat_stream_accepts_default_timezone_without_tzdata() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"message": "今天体重多少", "timezone": "Asia/Shanghai"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events[-1][0] == "done"


def test_chat_stream_rejects_oversized_user_id_header() -> None:
    app = create_app(debug_settings())

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "u" * 200, "Accept": "text/event-stream"},
            json={"message": "今天体重多少"},
        )

    assert response.status_code == 422


def test_chat_stream_emits_error_event_when_node_fails() -> None:
    import asyncio

    from weight_agent.api.schemas.chat import ChatRequest
    from weight_agent.chat.executor import RouteExecutor
    from weight_agent.chat.workflow import ChatWorkflow

    class FailingGreetingNode:
        async def execute(self, plan, context):
            raise RuntimeError("secret internal stack detail")

    workflow = ChatWorkflow(
        route_executor=RouteExecutor(greeting=FailingGreetingNode()),
        expose_debug_events=True,
    )

    async def collect() -> list[tuple[str, dict]]:
        events = []
        async for name, payload in workflow.run(
            ChatRequest(conversation_id="conv-fail", message="你好"),
            user_id="user-fail",
        ):
            events.append((name, payload))
        return events

    events = asyncio.run(collect())
    names = [name for name, _ in events]
    assert "done" not in names
    assert names[-1] == "error"
    error = events[-1][1]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["retryable"] is False
    assert "secret internal stack detail" not in error["message"]
    assert error["error_type"] == "RuntimeError"
    assert error["conversation_id"] == "conv-fail"


def test_chat_stream_maps_classifier_output_error() -> None:
    import asyncio

    from weight_agent.api.schemas.chat import ChatRequest
    from weight_agent.chat.intent import IntentClassificationError
    from weight_agent.chat.workflow import ChatWorkflow

    class FailingClassifier:
        async def classify(self, message, context):
            raise IntentClassificationError("model output invalid")

    workflow = ChatWorkflow(
        intent_classifier=FailingClassifier(),
        expose_debug_events=True,
    )

    async def collect() -> list[tuple[str, dict]]:
        events = []
        async for name, payload in workflow.run(
            ChatRequest(conversation_id="conv-model", message="今天体重多少"),
            user_id="user-model",
        ):
            events.append((name, payload))
        return events

    events = asyncio.run(collect())
    names = [name for name, _ in events]
    assert "done" not in names
    assert names[-1] == "error"
    assert events[-1][1]["code"] == "MODEL_OUTPUT_INVALID"
    assert events[-1][1]["retryable"] is True


def test_chat_stream_routes_chest_discomfort_to_blocked_safety_reply() -> None:
    import asyncio

    from weight_agent.api.schemas.chat import ChatRequest
    from weight_agent.chat.memory import InMemoryConversationMemory
    from weight_agent.chat.workflow import ChatWorkflow

    workflow = ChatWorkflow(
        memory=InMemoryConversationMemory(), expose_debug_events=True
    )

    async def collect() -> list[tuple[str, dict]]:
        events = []
        async for name, payload in workflow.run(
            ChatRequest(conversation_id="conv-safety", message="我胸口有点闷，要紧吗？"),
            user_id="user-safety",
        ):
            events.append((name, payload))
        return events

    events = asyncio.run(collect())
    names = [name for name, _ in events]
    assert names[-1] == "done"
    assert events[-1][1]["status"] == "blocked"
    route_execution = next(
        data for name, data in events if name == "data" and data["type"] == "route_execution"
    )
    assert route_execution["route_execution"]["route"] == "safety"
    assert route_execution["route_execution"]["status"] == "blocked"
    assert route_execution["route_execution"]["data"]["executed_nodes"] == ["safety_reply"]
    content = "".join(data["content"] for name, data in events if name == "delta")
    assert "医生" in content


def test_chat_stream_negated_symptom_skips_safety_route() -> None:
    import asyncio

    from weight_agent.api.schemas.chat import ChatRequest
    from weight_agent.chat.memory import InMemoryConversationMemory
    from weight_agent.chat.workflow import ChatWorkflow

    workflow = ChatWorkflow(
        memory=InMemoryConversationMemory(), expose_debug_events=True
    )

    async def collect() -> list[tuple[str, dict]]:
        events = []
        async for name, payload in workflow.run(
            ChatRequest(conversation_id="conv-negated", message="我没有胸痛"),
            user_id="user-negated",
        ):
            events.append((name, payload))
        return events

    events = asyncio.run(collect())
    supervisor_plan = next(
        data for name, data in events if name == "data" and data["type"] == "supervisor_plan"
    )
    assert supervisor_plan["supervisor_plan"]["route"] != "safety"


def test_encode_events_public_mode_passes_sanitized_error() -> None:
    import asyncio

    from weight_agent.api.sse import encode_events

    async def source():
        yield "start", {"conversation_id": "c1", "request_id": "r1"}
        yield "error", {
            "code": "INTERNAL_ERROR",
            "message": "服务暂时不可用，请稍后重试",
            "retryable": False,
            "error_type": "RuntimeError",
            "request_id": "r1",
            "conversation_id": "c1",
        }
        yield "done", {"status": "completed", "conversation_id": "c1"}

    async def collect() -> str:
        chunks = [chunk async for chunk in encode_events(source(), include_metadata=False)]
        return "".join(chunks)

    body = asyncio.run(collect())
    blocks = body.strip().split("\n\n")
    assert blocks[0].startswith("event: start")
    assert blocks[1].startswith("event: error")
    error_data = json.loads(blocks[1].splitlines()[1].removeprefix("data: "))
    assert set(error_data) == {"code", "message", "retryable"}
    assert "error_type" not in error_data
    assert "request_id" not in error_data
