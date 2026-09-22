import json

import httpx
import pytest

from weight_agent.api.schemas.report import ReportAnalysis, ReportRequest
from weight_agent.report.qwen import QwenReportAnalyzer, resolve_metric_references
from weight_agent.report.workflow import ReportAnalyzerError, ReportWorkflow


def make_request() -> ReportRequest:
    return ReportRequest.model_validate(
        {
            "measurement_id": "private-measurement-id",
            "measured_at": "2026-09-20T10:30:00+08:00",
            "body_composition": {
                "weight_kg": {
                    "value": 62.3,
                    "unit": "kg",
                    "level": "normal",
                    "standard_min": 55.3,
                    "standard_max": 74.9,
                },
                "muscle_mass_kg": {
                    "value": 44.7,
                    "unit": "kg",
                    "level": "low",
                    "standard_min": 47.0,
                    "standard_max": 64.6,
                },
            },
            "segmental_composition": {},
            "assessment": {
                "bmi": {"value": 21.1, "standard_min": 18.5, "standard_max": 23.0},
                "body_type": {"code": 4, "name": "浮肿肥胖型"},
                "muscle_control_kg": 7.3,
                "fat_control_kg": -4.5,
            },
            "exercise_calories_kcal_per_30_min": {},
            "segment_standards": {},
        }
    )


@pytest.mark.anyio
async def test_qwen_analyzer_sends_strict_schema_without_identifiers() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "overall_assessment": "体重正常，但肌肉量偏低。",
                                    "interventions": [
                                        {
                                            "category": "training",
                                            "content": "循序渐进进行抗阻训练。",
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/compatible-mode/v1/",
            model="qwen3.8-flash",
            request_timeout_seconds=5,
            temperature=0.35,
            max_completion_tokens=800,
            enable_thinking=True,
            client=client,
        )
        result = await analyzer.analyze(make_request(), user_id="private-user-id")

    assert result.overall_assessment == "体重正常，但肌肉量偏低。"
    assert captured["model"] == "qwen3.8-flash"
    assert captured["temperature"] == 0.35
    assert captured["max_completion_tokens"] == 800
    assert captured["enable_thinking"] is True
    assert captured["response_format"]["type"] == "json_schema"
    user_content = captured["messages"][1]["content"]
    assert "private-measurement-id" not in user_content
    assert "private-user-id" not in user_content
    assert "关键依据" not in user_content
    assert '"unit":"kg"' in user_content
    assert '"level":"low"' in user_content
    assert "浮肿肥胖型" in user_content


@pytest.mark.anyio
async def test_qwen_analyzer_rejects_invalid_model_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"unexpected": true}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/compatible-mode/v1",
            model="qwen3.8-flash",
            request_timeout_seconds=5,
            client=client,
        )
        with pytest.raises(ReportAnalyzerError):
            await analyzer.analyze(make_request(), user_id="user-id")


def test_metric_references_preserve_values_units_and_signs() -> None:
    request = make_request()
    analysis = ReportAnalysis(
        overall_assessment="体重 {{body_composition.weight_kg}}，BMI {{assessment.bmi}}。",
        interventions=[
            {
                "category": "direction",
                "content": "脂肪控制量 {{assessment.fat_control_kg}}，建议循序渐进减脂。",
            }
        ],
    )
    result = resolve_metric_references(request, analysis)
    assert result.overall_assessment == "体重 62.3kg，BMI 21.1。"
    assert "脂肪控制量 -4.5" in result.interventions[0].content
    assert "{{" in analysis.overall_assessment


@pytest.mark.parametrize(
    "content",
    [
        "体重为120kg。",
        "Your current weight is 120 kg.",
        "体重六十二公斤。",
        "体脂率百分之二十三。",
        "BMI 99。",
        "体重 {{body_composition.unknown}}。",
        "体重 {{body_composition.weight_kg.value}}。",
        "体脂率 {{assessment.body_fat_rate_percent}}。",
        "身体类型 {{assessment.body_type}}。",
        "测量 {{measurement_id.value}}。",
        "日期 {{measured_at.year}}。",
        "体重 {{body_composition.weight_kg}。",
    ],
)
def test_unverified_numeric_references_are_rejected(content) -> None:
    analysis = ReportAnalysis(overall_assessment=content, interventions=[])
    with pytest.raises(ValueError):
        resolve_metric_references(make_request(), analysis)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "model_output",
    [
        {"overall_assessment": "体重为120kg。", "interventions": []},
        {"overall_assessment": "体重 {{body_composition.unknown}}。", "interventions": []},
        {
            "overall_assessment": "体重正常。",
            "interventions": [{"category": "weight", "content": "体重120kg。"}],
        },
        {
            "overall_assessment": "体重正常。",
            "interventions": [
                {"category": "weight", "content": "增重"},
                {"category": "weight", "content": "减重"},
            ],
        },
    ],
)
async def test_unverified_or_duplicate_model_output_falls_back(model_output) -> None:
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(model_output, ensure_ascii=False)}}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer=analyzer, timeout_seconds=1).run(
            make_request(), "anonymous"
        )
    assert response.generation_mode == "fallback"
    assert len(response.interventions) == 6
    assert "120" not in response.overall_assessment


@pytest.mark.anyio
async def test_model_references_are_rendered_before_returning_report() -> None:
    def handler(request):
        payload = json.loads(request.content)
        assert "{{字段路径}}" in payload["messages"][0]["content"]
        assert "private-measurement-id" not in payload["messages"][1]["content"]
        assert "2026-09-20" not in payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "overall_assessment": (
                                        "体重 {{body_composition.weight_kg}}，肌肉量偏低。"
                                    ),
                                    "interventions": [
                                        {
                                            "category": "training",
                                            "content": "循序渐进进行抗阻训练。",
                                        }
                                    ],
                                }
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer=analyzer, timeout_seconds=1).run(
            make_request(), "anonymous"
        )
    assert response.generation_mode == "model"
    assert response.overall_assessment == "体重 62.3kg，肌肉量偏低。"
    assert len(response.interventions) == 6
    assert "{{" not in response.model_dump_json()
    assert response.key_evidence == []


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_upstream_http_failure_falls_back(status) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": "unavailable"})
        )
    ) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer=analyzer, timeout_seconds=1).run(
            make_request(), "anonymous"
        )
    assert response.generation_mode == "fallback"


@pytest.mark.anyio
async def test_http_client_timeout_is_logged_as_timeout(caplog) -> None:
    def handler(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    caplog.set_level("INFO", logger="weight_agent.report.workflow")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer=analyzer, timeout_seconds=1).run(
            make_request(), "anonymous"
        )
    assert response.generation_mode == "fallback"
    records = [
        record for record in caplog.records if record.message.startswith("report_request_completed")
    ]
    assert records[-1].fallback_reason == "timeout"


@pytest.mark.parametrize(
    "field",
    [
        "walking",
        "golf",
        "gateball",
        "tennis_cycling_basketball",
        "squash_shuttlecock_taekwondo_fencing",
        "climbing",
        "swimming_aerobics_jogging_football_jumping_rope",
        "badminton_table_tennis",
    ],
)
@pytest.mark.anyio
async def test_all_exercise_references_keep_model_generation(field):
    report_request = ReportRequest.model_validate(
        {"exercise_calories_kcal_per_30_min": {field: {"value": 124, "unit": "kCal/30min"}}}
    )
    content = "运动消耗参考 {{exercise_calories_kcal_per_30_min." + field + "}}。"
    output = {"overall_assessment": content, "interventions": []}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(output)}}]}
            )
        )
    ) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer, timeout_seconds=1).run(
            report_request, "anonymous"
        )
    assert response.generation_mode == "model"
    assert response.overall_assessment == "运动消耗参考 124kCal/30min。"
    assert len(response.interventions) == 6


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize(
    "content",
    [
        "身体得分一百分。",
        "BMI三十。",
        "体重一百二十。",
        "BMI为三。",
        "身体得分为零。",
        "体重壹佰贰拾公斤。",
        "体重 {{assessment.bmi}}，BMI {{body_composition.weight_kg}}。",
        "体重为 {{assessment.bmi}}。",
        "肌肉量 {{body_composition.weight_kg}}。",
        "BMI {{body_composition.muscle_mass_kg}}。",
    ],
)
@pytest.mark.anyio
async def test_chinese_numbers_and_obvious_label_mismatches_fall_back(location, content):
    output = {"overall_assessment": "肌肉量偏低。", "interventions": []}
    if location == "overall_assessment":
        output["overall_assessment"] = content
    else:
        output["interventions"] = [{"category": "body_status", "content": content}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(output)}}]}
            )
        )
    ) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer, timeout_seconds=1).run(
            make_request(), "anonymous"
        )
    assert response.generation_mode == "fallback"
    assert len(response.interventions) == 6


@pytest.mark.parametrize(
    "content",
    [
        "建议进一步改善肌肉量，保持规律饮食。",
        "身体年龄与实际年龄一致，注意两侧训练均衡。",
        "一般建议逐步增加运动，避免一味追求体重下降。",
        "营养十分重要，千万不要过度节食。",
        "万一出现不适，应停止运动。",
        "饮食十分均衡。",
        "万一感到不适，应停止运动。",
        "建议一一核对测量记录，身体年龄一致不代表整体健康。",
        "体重为 {{body_composition.weight_kg}}，体质指数 {{assessment.bmi}}。",
        "肌肉量偏低，体重 {{body_composition.weight_kg}}。",
        "体重与BMI分别为 {{body_composition.weight_kg}}、{{assessment.bmi}}。",
    ],
)
def test_qualitative_chinese_and_valid_label_references_remain_supported(content):
    result = resolve_metric_references(
        make_request(), ReportAnalysis(overall_assessment=content, interventions=[])
    )
    assert result.overall_assessment
    assert "{{" not in result.overall_assessment


def test_segment_and_extended_impedance_references_remain_supported():
    request = ReportRequest.model_validate(
        {
            "segmental_composition": {"muscle_mass_kg": {"left_arm": {"value": 2, "unit": "kg"}}},
            "bioelectrical_impedance": {"50kHz_left_arm": {"value": 250, "unit": "Ω"}},
            "assessment": {"ideal_body_weight_kg": 60, "weight_control_kg": -2},
        }
    )
    result = resolve_metric_references(
        request,
        ReportAnalysis(
            overall_assessment=(
                "左臂肌肉量 {{segmental_composition.muscle_mass_kg.left_arm}}，"
                "阻抗 {{bioelectrical_impedance.50kHz_left_arm}}，"
                "理想体重 {{assessment.ideal_body_weight_kg}}，"
                "体重控制量 {{assessment.weight_control_kg}}。"
            ),
            interventions=[],
        ),
    )
    assert result.overall_assessment == "左臂肌肉量 2kg，阻抗 250Ω，理想体重 60，体重控制量 -2。"


@pytest.mark.anyio
async def test_model_zero_intake_reference_uses_safe_fallback():
    report_request = ReportRequest.model_validate(
        {"assessment": {"recommended_calorie_intake_kcal": {"value": 0, "unit": "kCal"}}}
    )
    output = {
        "overall_assessment": "请关注营养摄入。",
        "interventions": [
            {
                "category": "nutrition",
                "content": "每日摄入参考 {{assessment.recommended_calorie_intake_kcal}}。",
            }
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(output)}}]}
            )
        )
    ) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer, timeout_seconds=1).run(
            report_request, "anonymous"
        )
    assert response.generation_mode == "fallback"
    assert "0kCal" not in response.interventions[2].content


@pytest.mark.anyio
async def test_complete_sample_http_to_mock_model_and_all_metric_references():
    import runpy
    from pathlib import Path

    from weight_agent.core.config import Settings
    from weight_agent.main import create_app

    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_report_models.py"
    report_request = runpy.run_path(str(script), run_name="sample_test")["sample_request"]()
    pending = [("", report_request.model_dump())]
    count = 0
    while pending:
        prefix, value = pending.pop()
        if not isinstance(value, dict):
            continue
        if value.get("value") is not None:
            content = "指标参考 {{" + prefix + "}}。"
            resolved = resolve_metric_references(
                report_request, ReportAnalysis(overall_assessment=content, interventions=[])
            )
            assert "{{" not in resolved.overall_assessment, prefix
            count += 1
        else:
            pending.extend(
                (f"{prefix}.{key}" if prefix else key, item) for key, item in value.items()
            )
    assert count >= 50

    output = {
        "overall_assessment": (
            "体重 {{body_composition.weight_kg}}，BMI {{assessment.bmi}}；"
            "步行消耗参考 {{exercise_calories_kcal_per_30_min.walking}}。"
        ),
        "interventions": [{"category": "training", "content": "循序渐进进行抗阻训练。"}],
    }

    def handler(request):
        sent = json.loads(request.content)["messages"][1]["content"]
        assert "private-user" not in sent
        assert "measurement_id" not in sent and "measured_at" not in sent
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as model_client:
        app = create_app(
            Settings(_env_file=None, environment="test", dashscope_api_key=None),
            report_analyzer=QwenReportAnalyzer(
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="mock",
                request_timeout_seconds=1,
                client=model_client,
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/report",
                json=report_request.model_dump(mode="json"),
                headers={"X-User-Id": "private-user"},
            )
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {
        "request_id",
        "status",
        "generation_mode",
        "overall_assessment",
        "key_evidence",
        "interventions",
    }
    assert result["generation_mode"] == "model"
    assert len(result["interventions"]) == 6
    assert result["key_evidence"] == []
    assert "{{" not in response.text
    assert response.headers["X-Response-Time-Ms"]
