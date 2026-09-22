import asyncio
import runpy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weight_agent.api.schemas.report import (
    Intervention,
    ReportAnalysis,
    ReportRequest,
)
from weight_agent.core.config import Settings
from weight_agent.main import create_app
from weight_agent.report.qwen import QwenReportAnalyzer
from weight_agent.report.workflow import UnavailableReportAnalyzer


def report_payload(**assessment_overrides: object) -> dict:
    assessment = {
        "muscle_control_kg": 7.3,
        "fat_control_kg": -4.5,
        "ideal_body_weight_kg": 65.1,
        "weight_control_kg": 2.8,
        "recommended_calorie_intake_kcal": 1827,
    }
    assessment.update(assessment_overrides)
    return {
        "measurement_id": "measurement-123",
        "body_composition": {},
        "segmental_composition": {},
        "assessment": assessment,
        "exercise_calories_kcal_per_30_min": {},
        "segment_standards": {},
    }


def test_report_uses_unavailable_analyzer_without_network_in_test_mode() -> None:
    app = create_app(Settings(environment="test", dashscope_api_key=None))

    assert isinstance(app.state.report_workflow._analyzer, UnavailableReportAnalyzer)


def test_report_uses_all_matching_fallback_rules() -> None:
    app = create_app(Settings(environment="test", dashscope_api_key=None))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/report",
            headers={"X-User-Id": "user-123"},
            json=report_payload(),
        )

    assert response.status_code == 200
    result = response.json()
    assert result["generation_mode"] == "fallback"
    assert result["key_evidence"] == []
    assert result["interventions"] == [
        {
            "category": "direction",
            "content": (
                "以增肌（肌肉控制量 +7.3kg）、减脂（脂肪控制量 -4.5kg）为核心，"
                "按身体成分变化调整重点。"
            ),
        },
        {
            "category": "training",
            "content": (
                "循序渐进进行抗阻训练，以支持肌肉增长；结合自身耐受情况安排有氧活动，以支持减脂。"
            ),
        },
        {
            "category": "nutrition",
            "content": "每日摄入参考 1827kCal，优先保证蛋白质和规律饮食。",
        },
        {
            "category": "weight",
            "content": (
                "理想体重 65.1kg，体重控制量 +2.8kg，表示建议增加体重；"
                "应结合肌肉与脂肪变化，不只看体重升降。"
            ),
        },
        {
            "category": "retest",
            "content": "建议 4 周后复测，重点观察体脂率、肌肉量及本次干预目标的变化。",
        },
        {
            "category": "body_status",
            "content": "暂无足够身体状态参考数据，建议补充身体得分、身体年龄或身体类型。",
        },
    ]


def test_fallback_body_status_contains_comparative_analysis() -> None:
    app = create_app(Settings(environment="test", dashscope_api_key=None))
    payload = report_payload(
        body_score={"value": 66, "unit": "分"},
        body_age={"value": 19, "unit": "岁"},
        body_type={"code": 4, "name": "浮肿肥胖型"},
    )
    payload["subject"] = {"age_years": {"value": 28, "unit": "岁"}}
    payload["body_composition"] = {
        "muscle_mass_kg": {"value": 44.7, "unit": "kg", "level": "low"},
        "total_body_water_kg": {"value": 35.2, "unit": "kg", "level": "low"},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=payload)

    assert response.status_code == 200
    body_status = response.json()["interventions"][-1]["content"]
    assert "身体得分 66 分" in body_status
    assert "较实际年龄 28 岁年轻 9 岁" in body_status
    assert "身体类型为浮肿肥胖型" in body_status
    assert "肌肉量" in body_status


def test_report_uses_default_when_no_fallback_rule_matches() -> None:
    app = create_app(Settings(environment="test", dashscope_api_key=None))
    payload = report_payload(
        muscle_control_kg=0,
        fat_control_kg=0,
        weight_control_kg=0,
        recommended_calorie_intake_kcal=None,
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=payload)

    assert response.status_code == 200
    categories = [item["category"] for item in response.json()["interventions"]]
    assert categories == ["direction", "training", "nutrition", "weight", "retest", "body_status"]
    assert response.headers["X-Response-Time-Ms"]


class SlowAnalyzer:
    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis:
        await asyncio.sleep(0.05)
        return ReportAnalysis(
            overall_assessment="模型总结",
            interventions=[Intervention(category="training", content="模型建议")],
        )


def test_report_falls_back_when_model_times_out(caplog) -> None:
    app = create_app(
        Settings(environment="test", report_model_timeout_seconds=0.001),
        report_analyzer=SlowAnalyzer(),
    )
    caplog.set_level("INFO", logger="weight_agent.report.workflow")

    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=report_payload())

    assert response.status_code == 200
    assert response.json()["generation_mode"] == "fallback"
    records = [r for r in caplog.records if r.message.startswith("report_request_completed")]
    assert len(records) == 1
    assert records[0].fallback_reason == "timeout"
    assert records[0].model_duration_ms >= 0
    assert records[0].total_duration_ms >= records[0].model_duration_ms


class SuccessfulAnalyzer:
    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis:
        assert user_id == "user-123"
        return ReportAnalysis(
            overall_assessment="模型总结",
            interventions=[Intervention(category="training", content="模型建议")],
        )


class PartialAnalyzer:
    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis:
        del request, user_id
        return ReportAnalysis(
            overall_assessment="模型整体分析",
            interventions=[
                Intervention(category="training", content="模型训练建议"),
                Intervention(category="body_status", content="模型身体状态"),
            ],
        )


def test_report_merges_only_missing_categories_with_fallback() -> None:
    app = create_app(
        Settings(environment="test", dashscope_api_key=None),
        report_analyzer=PartialAnalyzer(),
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=report_payload())

    assert response.status_code == 200
    result = response.json()
    assert result["generation_mode"] == "model"
    assert result["overall_assessment"] == "模型整体分析"
    assert [item["category"] for item in result["interventions"]] == [
        "direction",
        "training",
        "nutrition",
        "weight",
        "retest",
        "body_status",
    ]
    assert result["interventions"][1] == {
        "category": "training",
        "content": "模型训练建议",
    }
    assert result["interventions"][5] == {
        "category": "body_status",
        "content": "模型身体状态",
    }
    assert result["interventions"][0]["category"] == "direction"
    assert "肌肉控制量" in result["interventions"][0]["content"]


def test_report_returns_model_analysis_and_logs_duration(caplog) -> None:
    app = create_app(Settings(environment="test"), report_analyzer=SuccessfulAnalyzer())
    caplog.set_level("INFO", logger="weight_agent.report.workflow")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/report",
            headers={"X-User-Id": "user-123"},
            json=report_payload(),
        )

    assert response.status_code == 200
    assert response.json()["generation_mode"] == "model"
    assert response.json()["overall_assessment"] == "模型总结"
    assert response.json()["key_evidence"] == []
    interventions = response.json()["interventions"]
    assert [item["category"] for item in interventions] == [
        "direction",
        "training",
        "nutrition",
        "weight",
        "retest",
        "body_status",
    ]
    assert interventions[1] == {"category": "training", "content": "模型建议"}
    records = [r for r in caplog.records if r.message.startswith("report_request_completed")]
    assert len(records) == 1
    assert records[0].generation_mode == "model"
    assert records[0].fallback_reason is None
    assert records[0].model_duration_ms >= 0
    assert records[0].total_duration_ms >= records[0].model_duration_ms


def test_report_rejects_unknown_fields() -> None:
    app = create_app(Settings(environment="test", dashscope_api_key=None))
    payload = report_payload()
    payload["assessment"]["unknown_metric"] = 1

    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=payload)

    assert response.status_code == 422


def test_report_accepts_partial_body270_input_and_returns_backend_evidence() -> None:
    class StaticEvidenceProvider:
        def build(self, request: ReportRequest) -> list[dict[str, str]]:
            del request
            return [{"tag": "BMI", "text": "BMI 处于参考范围内", "cls": "ok"}]

    app = create_app(
        Settings(environment="test", dashscope_api_key=None),
        key_evidence_provider=StaticEvidenceProvider(),
    )
    payload = {
        "assessment": {"bmi": {"value": 21.1, "standard_min": 18.5, "standard_max": 23.0}},
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=payload)

    assert response.status_code == 200
    assert response.json()["key_evidence"] == [
        {"tag": "BMI", "text": "BMI 处于参考范围内", "cls": "ok"}
    ]


def test_report_accepts_metric_metadata_and_body_type_code() -> None:
    request = ReportRequest.model_validate(
        {
            "subject": {
                "sex": "male",
                "height_cm": {"value": 172, "unit": "cm"},
                "age_years": {"value": 28, "unit": "岁"},
            },
            "body_composition": {"subcutaneous_fat_mass_kg": {"value": 12.9, "unit": "kg"}},
            "segmental_composition": {
                "fat_rate_percent": {"trunk": {"value": 25.6, "unit": "%", "level": "high"}}
            },
            "assessment": {
                "body_type": {"code": 4, "name": "浮肿肥胖型"},
                "bmi": {
                    "value": 21.1,
                    "unit": "kg/m2",
                    "level": "normal",
                    "standard_min": 18.5,
                    "standard_max": 23.0,
                },
            },
            "exercise_calories_kcal_per_30_min": {"walking": {"value": 124, "unit": "kCal/30min"}},
            "segment_standards": {"fat": {"trunk": 2}},
        }
    )

    assert request.subject.height_cm.value == 172
    assert request.body_composition.subcutaneous_fat_mass_kg.unit == "kg"
    assert request.segmental_composition.fat_rate_percent.trunk.level == "high"
    assert request.assessment.body_type.code == 4
    assert request.assessment.body_type.name == "浮肿肥胖型"
    assert request.segment_standards.fat.trunk == 2


def test_report_accepts_full_report_data_groups() -> None:
    request = ReportRequest.model_validate(
        {
            "subject": {
                "sex": "female",
                "height_cm": {"value": 165, "unit": "cm"},
                "age_years": {"value": 35, "unit": "岁"},
            },
            "bioelectrical_impedance": {
                "right_arm": {"value": 281, "unit": "ohm"},
                "left_arm": {"value": 276, "unit": "ohm"},
                "trunk": {"value": 24, "unit": "ohm"},
                "right_leg": {"value": 310, "unit": "ohm"},
                "left_leg": {"value": 307, "unit": "ohm"},
                "device_extra": {"value": 123},
            },
            "body_composition": {
                "weight_kg": {"value": 62.3, "unit": "kg", "level": "normal"},
                "total_body_water_kg": {"value": 35.2, "unit": "kg", "level": "low"},
                "muscle_mass_kg": {
                    "value": 44.7,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 47,
                    "standard_max": 64.6,
                },
            },
            "segmental_composition": {
                "fat_rate_percent": {"trunk": {"value": 25.6, "unit": "%", "level": "high"}},
                "muscle_rate_percent": {"left_arm": {"value": 35.6, "unit": "%", "level": "low"}},
            },
            "assessment": {
                "body_score": {"value": 66, "unit": "分"},
                "body_age": {"value": 19, "unit": "岁"},
                "body_type": {"code": 4, "name": "浮肿肥胖型"},
                "bmi": {"value": 21.1, "unit": "kg/m²"},
                "basal_metabolism_kcal": {"value": 1340, "unit": "kCal/day"},
            },
            "exercise_calories_kcal_per_30_min": {
                "walking": {"value": 124, "unit": "kCal/30min"},
                "climbing": {"value": 203, "unit": "kCal/30min"},
            },
            "segment_standards": {
                "fat": {"trunk": 2},
                "muscle": {"left_arm": 0},
            },
        }
    )

    assert request.subject.sex == "female"
    assert request.bioelectrical_impedance.trunk.value == 24
    assert request.bioelectrical_impedance.model_extra["device_extra"].value == 123
    assert request.assessment.body_score.value == 66
    assert request.exercise_calories_kcal_per_30_min.climbing.value == 203


def test_app_configures_qwen_when_api_key_is_present() -> None:
    app = create_app(
        Settings(
            environment="test",
            DASHSCOPE_API_KEY="test-key",
            report_model_temperature=0.35,
            report_model_max_completion_tokens=800,
            report_model_enable_thinking=True,
        )
    )

    assert isinstance(app.state.report_workflow._analyzer, QwenReportAnalyzer)
    analyzer = app.state.report_workflow._analyzer
    assert analyzer._temperature == 0.35
    assert analyzer._max_completion_tokens == 800
    assert analyzer._enable_thinking is True


@pytest.mark.parametrize("api_key", [None, "", "   ", "\t\n"])
def test_blank_api_key_uses_offline_fallback(api_key) -> None:
    settings = Settings(_env_file=None, environment="test", dashscope_api_key=api_key)
    app = create_app(settings)
    assert settings.dashscope_api_key is None
    assert isinstance(app.state.report_workflow._analyzer, UnavailableReportAnalyzer)
    with TestClient(app) as client:
        response = client.post("/api/v1/report", json={})
    assert response.status_code == 200
    assert response.json()["generation_mode"] == "fallback"


@pytest.mark.parametrize("key_name", ["DASHSCOPE_API_KEY", "WEIGHT_AGENT_DASHSCOPE_API_KEY"])
def test_blank_environment_key_is_unconfigured(monkeypatch, key_name) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("WEIGHT_AGENT_DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv(key_name, " ")
    assert Settings(_env_file=None).dashscope_api_key is None


@pytest.mark.parametrize(
    "payload",
    [
        {"body_composition": {"weight_kg": -1}},
        {"body_composition": {"weight_kg": 0}},
        {"body_composition": {"weight_kg": {"value": 60, "unit": "cm"}}},
        {"body_composition": {"weight_kg": {"standard_min": 80, "standard_max": 50}}},
        {"body_composition": {"muscle_mass_kg": {"standard_min": -1}}},
        {"assessment": {"muscle_control_kg": "1e500"}},
        {"assessment": {"muscle_control_kg": "1e-500"}},
        {"assessment": {"muscle_control_kg": "NaN"}},
        {"assessment": {"muscle_control_kg": "Infinity"}},
        {"assessment": {"muscle_control_kg": True}},
        {"assessment": {"bmi": {"level": True}}},
        {"assessment": {"body_type": []}},
        {"assessment": {"body_type": True}},
        {"assessment": {"body_type": 1.5}},
        {"assessment": {"body_type": {"code": True}}},
        {"assessment": {"body_type": {"code": "1"}}},
        {"assessment": {"body_type": {"code": 1.0}}},
        {"assessment": {"body_type": {"name": "名" * 101}}},
        {"assessment": {"recommended_calorie_intake_kcal": -1}},
        {"assessment": {"bmi": {"unit": "kg"}}},
        {"subject": {"height_cm": 0}},
        {"subject": {"age_years": -1}},
        {"segment_standards": {"fat": {"right_arm": True}}},
        {"segment_standards": {"fat": {"right_arm": 1.0}}},
        {"segment_standards": {"fat": {"right_arm": "1"}}},
        {"segment_standards": {"fat": {"right_arm": 3}}},
        {"bioelectrical_impedance": {"device_extra": {"value": 10, "unknown": True}}},
        {"bioelectrical_impedance": {"device_extra": "not a metric"}},
        {"bioelectrical_impedance": {"right_arm": {"value": 10, "unit": "kg"}}},
        {"segmental_composition": {"fat_mass_kg": {"trunk": -1}}},
        {"segmental_composition": {"fat_rate_percent": {"trunk": {"unit": "kg"}}}},
        {"exercise_calories_kcal_per_30_min": {"walking": -1}},
    ],
)
def test_invalid_report_metrics_return_422(payload) -> None:
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/v1/report", json=payload)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"assessment": {"muscle_control_kg": -1, "fat_control_kg": 1, "weight_control_kg": 0}},
        {"body_composition": {"weight_kg": {"value": 60, "standard_min": 60, "standard_max": 60}}},
        {"assessment": {"bmi": {"value": 21.1, "unit": "kg/m²"}}},
        {"assessment": {"skeletal_muscle_index": {"value": 7.8, "unit": "设备指数"}}},
        {"assessment": {"obesity_degree_percent": {"value": 120, "unit": "%"}}},
        {"assessment": {"body_type": 4}},
        {"assessment": {"body_type": "标准型"}},
        {"body_composition": {"weight_kg": None}},
        {"segment_standards": {"fat": {"right_arm": 0, "left_arm": 1, "trunk": 2}}},
        {"segmental_composition": {"muscle_rate_percent": {"trunk": 120}}},
        {"bioelectrical_impedance": {"device_extra": {"value": 123, "unit": "Ω"}}},
        {"assessment": {"recommended_calorie_intake_kcal": {"value": 1800, "unit": "kcal/day"}}},
    ],
)
def test_valid_optional_and_boundary_metrics_remain_accepted(payload) -> None:
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=payload)
    assert response.status_code == 200, response.text
    assert len(response.json()["interventions"]) == 6


@pytest.mark.parametrize("control, expected", [(-5, "-5kg"), (5, "+5kg"), (0, "无需据此调整体重")])
def test_weight_control_preserves_direction(control, expected) -> None:
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=report_payload(weight_control_kg=control))
    content = next(
        x["content"] for x in response.json()["interventions"] if x["category"] == "weight"
    )
    assert expected in content
    if control:
        assert ("减少体重" if control < 0 else "增加体重") in content


@pytest.mark.parametrize(
    "muscle, fat, resistance, aerobic",
    [
        (1, -1, True, True),
        (1, 0, True, False),
        (0, -1, False, True),
        (0, 0, False, False),
        (-1, 1, False, False),
        (None, None, False, False),
    ],
)
def test_training_only_uses_matching_control_rules(muscle, fat, resistance, aerobic) -> None:
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/report", json=report_payload(muscle_control_kg=muscle, fat_control_kg=fat)
        )
    content = next(
        x["content"] for x in response.json()["interventions"] if x["category"] == "training"
    )
    assert ("抗阻训练" in content) is resistance
    assert ("有氧活动" in content) is aerobic
    assert "45" not in content
    if muscle == fat == 0:
        assert "无需据此新增" in content


def test_complete_model_report_does_not_build_fallback(monkeypatch) -> None:
    from weight_agent.report import workflow

    class CompleteAnalyzer:
        async def analyze(self, request, user_id):
            return ReportAnalysis(
                overall_assessment="模型总结",
                interventions=[
                    Intervention(category=c, content="模型建议")
                    for c in reversed(workflow.REQUIRED_INTERVENTION_CATEGORIES)
                ],
            )

    def unexpected_fallback(request):
        pytest.fail("Complete model report must not build fallback")

    monkeypatch.setattr(workflow, "build_fallback_analysis", unexpected_fallback)
    app = create_app(
        Settings(_env_file=None, environment="test", dashscope_api_key=None),
        report_analyzer=CompleteAnalyzer(),
    )
    with TestClient(app) as client:
        result = client.post("/api/v1/report", json={}).json()
    assert result["generation_mode"] == "model"
    assert [x["category"] for x in result["interventions"]] == list(
        workflow.REQUIRED_INTERVENTION_CATEGORIES
    )


def test_benchmark_sample_matches_current_request_schema() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_report_models.py"
    namespace = runpy.run_path(str(script), run_name="benchmark_test")
    request = namespace["sample_request"]()
    assert request.subject.sex == "male"
    assert request.assessment.muscle_control_kg.value is not None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
@pytest.mark.parametrize(
    "template",
    [
        '{"assessment":{"bmi":%s}}',
        '{"assessment":{"bmi":{"value":%s}}}',
        '{"assessment":{"bmi":{"standard_min":%s}}}',
        '{"assessment":{"body_type":{"code":%s}}}',
        '{"unknown":{"nested":[%s]}}',
    ],
)
def test_nonfinite_raw_json_returns_serializable_422(literal, template) -> None:
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/report",
            content=template % literal,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"][0] == "body"
    assert response.headers["X-Response-Time-Ms"]


@pytest.mark.parametrize(
    "code, name",
    list(
        enumerate(
            [
                "偏瘦型",
                "偏瘦肌肉型",
                "肌肉发达型",
                "浮肿肥胖型",
                "偏胖肌肉型",
                "肌肉型偏胖",
                "缺乏运动型",
                "标准型",
                "标准肌肉型",
            ],
            start=1,
        )
    ),
)
def test_body_type_code_and_name_must_agree(code, name) -> None:
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app) as client:
        valid = client.post(
            "/api/v1/report", json={"assessment": {"body_type": {"code": code, "name": name}}}
        )
        invalid = client.post(
            "/api/v1/report",
            json={"assessment": {"body_type": {"code": code, "name": "不匹配名称"}}},
        )
    assert valid.status_code == 200
    assert invalid.status_code == 422
    assert invalid.json()["detail"][0]["loc"] == ["body", "assessment", "body_type"]


@pytest.mark.parametrize("body_type", [None, {}, 1, "自定义类型", {"code": 1}, {"name": "标准型"}])
def test_partial_body_type_remains_supported(body_type) -> None:
    request = ReportRequest.model_validate({"assessment": {"body_type": body_type}})
    assert request.assessment.body_type is not None


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize(
    "group, field, metric, expected",
    [
        ("body_composition", "muscle_mass_kg", {"value": 20, "standard_min": 40}, "肌肉量"),
        ("body_composition", "mineral_mass_kg", {"value": 1, "level": "low"}, "无机盐"),
        ("assessment", "body_fat_rate_percent", {"value": 45, "level": "high"}, "体脂率"),
        ("assessment", "visceral_fat_level", {"value": 15, "standard_max": 10}, "内脏脂肪"),
        ("assessment", "bmi", {"value": 30, "level": "偏高"}, "BMI"),
    ],
)
def test_body_status_uses_supplied_abnormal_evidence(use_model, group, field, metric, expected):
    payload = {"subject": {"age_years": 40}, "assessment": {"body_age": 30}}
    payload.setdefault(group, {})[field] = metric
    app = create_app(
        Settings(_env_file=None, environment="test", dashscope_api_key=None),
        report_analyzer=SuccessfulAnalyzer() if use_model else None,
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/report", json=payload, headers={"X-User-Id": "user-123"})
    assert response.status_code == 200
    result = response.json()
    assert result["generation_mode"] == ("model" if use_model else "fallback")
    status = result["interventions"][-1]["content"]
    assert expected in status
    assert "改善重点" in status
    assert "年轻 10 岁" in status
    assert "保持" not in status


@pytest.mark.parametrize(
    "metric, abnormal",
    [
        ({"level": "low"}, False),
        ({"standard_min": 40}, False),
        ({"value": 20}, False),
        ({"value": 20, "level": "normal", "standard_min": 40}, False),
        ({"value": 20, "level": "unknown", "standard_min": 40}, False),
        ({"value": 20, "level": 0, "standard_min": 40}, False),
        ({"value": 40, "standard_min": 40, "standard_max": 60}, False),
        ({"value": 60, "standard_min": 40, "standard_max": 60}, False),
        ({"value": 20, "level": "", "standard_min": 40}, True),
        ({"value": 50, "level": "low", "standard_min": 40, "standard_max": 60}, True),
        ({"value": 70, "standard_max": 60}, True),
    ],
)
def test_body_status_requires_value_and_prioritizes_explicit_level(metric, abnormal):
    from weight_agent.report.workflow import build_fallback_analysis

    request = ReportRequest.model_validate(
        {
            "subject": {"age_years": 40},
            "assessment": {"body_age": 30},
            "body_composition": {"muscle_mass_kg": metric},
        }
    )
    status = build_fallback_analysis(request).interventions[-1].content
    assert ("改善重点" in status) is abnormal
    assert "保持" not in status


@pytest.mark.parametrize(
    "metric, standard, abnormal",
    [
        ({"value": 2, "level": "low"}, None, True),
        ({"value": 2, "standard_min": 3}, None, True),
        ({"value": 2}, 0, True),
        ({"value": 2}, 2, True),
        ({"value": 2}, 1, False),
        ({}, 0, False),
        ({"value": 2, "level": "normal"}, 0, False),
        ({"value": 2, "standard_min": 2}, 0, False),
    ],
)
def test_body_status_uses_segment_evidence_only_with_measurement(metric, standard, abnormal):
    from weight_agent.report.workflow import build_fallback_analysis

    request = ReportRequest.model_validate(
        {
            "segmental_composition": {"muscle_rate_percent": {"left_arm": metric}},
            "segment_standards": {"muscle": {"left_arm": standard}},
        }
    )
    status = build_fallback_analysis(request).interventions[-1].content
    assert ("改善重点" in status) is abnormal
    if abnormal:
        assert "左" in status and "肌肉" in status


@pytest.mark.parametrize("use_model", [False, True])
@pytest.mark.parametrize("intake", [0, "0", {"value": 0, "unit": "kcal/day"}])
def test_zero_intake_is_not_a_daily_target(intake, use_model):
    app = create_app(
        Settings(_env_file=None, environment="test", dashscope_api_key=None),
        report_analyzer=SuccessfulAnalyzer() if use_model else None,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/report",
            json=report_payload(recommended_calorie_intake_kcal=intake),
            headers={"X-User-Id": "user-123"},
        )
    assert response.status_code == 200
    result = response.json()
    assert len(result["interventions"]) == 6
    nutrition = result["interventions"][2]["content"]
    assert "0kCal" not in nutrition
    assert "暂无足够营养参考数据" in nutrition
    assert "肌肉控制量 +7.3kg" in result["interventions"][0]["content"]


def test_regular_validation_error_keeps_fastapi_detail_contract():
    app = create_app(Settings(_env_file=None, environment="test", dashscope_api_key=None))
    with TestClient(app) as client:
        response = client.post("/api/v1/report", json={"assessment": {"bmi": "invalid"}})
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "decimal_parsing",
            "loc": ["body", "assessment", "bmi", "value"],
            "msg": "Input should be a valid decimal",
            "input": "invalid",
        }
    ]


def test_segment_ratio_rating_is_not_applied_to_mass():
    from weight_agent.report.workflow import build_fallback_analysis

    request = ReportRequest.model_validate(
        {
            "segmental_composition": {"muscle_mass_kg": {"left_arm": 2}},
            "segment_standards": {"muscle": {"left_arm": 0}},
        }
    )
    assert "改善重点" not in build_fallback_analysis(request).interventions[-1].content
