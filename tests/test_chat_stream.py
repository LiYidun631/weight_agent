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


def test_chat_stream_event_contract() -> None:
    app = create_app(Settings(environment="test"))

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
    assert [name for name, _ in events] == [
        "start",
        "progress",
        "data",
        "data",
        "data",
        "delta",
        "done",
    ]
    assert [data["sequence"] for _, data in events] == [1, 2, 3, 4, 5, 6, 7]
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
    assert events[-1][1] == {
        **{key: events[-1][1][key] for key in ("request_id", "sequence", "timestamp")},
        "status": "completed",
        "conversation_id": "conv-existing",
    }


def test_chat_stream_returns_clarification_when_intent_is_ambiguous() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"conversation_id": "conv-existing", "message": "帮我看看"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "start",
        "progress",
        "data",
        "data",
        "data",
        "progress",
        "delta",
        "done",
    ]
    assert events[2][1]["intent_result"]["needs_clarification"] is True
    assert events[3][1]["supervisor_plan"]["route"] == "clarification"
    assert events[4][1]["route_execution"]["status"] == "needs_clarification"
    assert events[6][1]["content"] == events[2][1]["intent_result"]["clarification_question"]
    assert events[-1][1]["status"] == "needs_clarification"


def test_chat_stream_passes_request_timezone_to_route_execution() -> None:
    app = create_app(Settings(environment="test", chat_default_timezone="UTC"))

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


def test_chat_stream_routes_greeting() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123", "Accept": "text/event-stream"},
            json={"conversation_id": "conv-existing", "message": "你好"},
        )

    assert response.status_code == 200
    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "start",
        "progress",
        "data",
        "data",
        "data",
        "delta",
        "done",
    ]
    assert events[2][1]["intent_result"]["intent"] == "greeting"
    assert events[2][1]["intent_result"]["domain"] == "general"
    assert events[3][1]["supervisor_plan"]["route"] == "greeting"
    assert events[4][1]["route_execution"]["data"]["executed_nodes"] == ["greeting_reply"]
    assert "体重管理" in events[5][1]["content"]


def test_chat_stream_rejects_blank_message() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-User-Id": "user-123"},
            json={"message": "   "},
        )

    assert response.status_code == 422
