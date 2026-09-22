from fastapi.testclient import TestClient

from weight_agent.core.config import PROJECT_ROOT, Settings
from weight_agent.main import create_app


def test_health_check() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Weight Agent API",
        "version": "0.1.0",
        "environment": "test",
    }


def test_openapi_is_available() -> None:
    app = create_app(Settings(environment="test"))

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Weight Agent API"


def test_server_settings_have_safe_defaults_and_validate_port() -> None:
    settings = Settings(environment="test")

    assert settings.host == "127.0.0.1"
    assert 1 <= settings.port <= 65535
    assert isinstance(settings.reload, bool)


def test_settings_use_project_root_env_file() -> None:
    assert PROJECT_ROOT.name == "Weight_agent"
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"
