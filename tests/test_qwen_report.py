import json
import logging
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from weight_agent.api.schemas.report import ReportAnalysis, ReportRequest, ReportResponse
from weight_agent.report.qwen import (
    QwenReportAnalyzer,
    _ModelAnalysis,
    _reference_data,
    resolve_metric_references,
)
from weight_agent.report.workflow import ReportAnalyzerError, ReportWorkflow

CATEGORIES = ["direction", "training", "nutrition", "weight", "retest", "body_status"]


def fallback_content(request: ReportRequest, category: str) -> str:
    from weight_agent.report.workflow import build_fallback_analysis

    return next(
        item.content
        for item in build_fallback_analysis(request).interventions
        if item.category == category
    )


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


def referenced_text(text: str, references: Sequence[str] = ()) -> dict[str, Any]:
    return {"text": text, "references": list(references)}


def model_output(
    text: str = "肌肉量偏低。",
    references: Sequence[str] = (),
    *,
    location: str = "overall_assessment",
) -> dict[str, Any]:
    block = referenced_text(text, references)
    if location == "overall_assessment":
        return {"overall_assessment": block, "interventions": []}
    return {
        "overall_assessment": referenced_text("肌肉量偏低。"),
        "interventions": [{"category": "body_status", "content": block}],
    }


def metric_id(request: ReportRequest, *keys: str) -> str:
    # 按本次请求的原始字典键取编号，不硬编码编号，也不拆分含点的扩展名称。
    node, _ = _reference_data(request)
    for key in keys:
        node = node[key]
    return node["reference_id"]


def model_response(output: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]},
    )


async def run_model_output(request: ReportRequest, output: dict[str, Any]) -> ReportResponse:
    calls = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        return model_response(output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        response = await ReportWorkflow(analyzer, timeout_seconds=1).run(request, "anonymous")
    assert len(calls) == 1
    assert [item.category for item in response.interventions] == CATEGORIES
    assert response.key_evidence == []
    return response


@pytest.mark.anyio
async def test_qwen_analyzer_sends_strict_schema_without_identifiers() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert str(request.url) == "https://example.invalid/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["Content-Type"] == "application/json"
        assert set(request.extensions["timeout"].values()) == {5}
        return model_response(
            {
                "overall_assessment": referenced_text("体重正常，但肌肉量偏低。"),
                "interventions": [
                    {"category": "training", "content": referenced_text("循序渐进进行抗阻训练。")}
                ],
            }
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
        assert analyzer.prompt_version == "report-v8"
        assert analyzer.model_name == "qwen3.8-flash"

    assert isinstance(result, ReportAnalysis)
    assert result.overall_assessment == "体重正常，但肌肉量偏低。"
    assert result.interventions[0].content == "循序渐进进行抗阻训练。"
    assert captured["model"] == "qwen3.8-flash"
    assert captured["temperature"] == 0.35
    assert captured["max_completion_tokens"] == 800
    assert captured["enable_thinking"] is True
    assert captured["response_format"]["type"] == "json_schema"
    schema_config = captured["response_format"]["json_schema"]
    assert schema_config["strict"] is True
    assert schema_config["name"] == "body_composition_report"
    schema = schema_config["schema"]
    assert schema == _ModelAnalysis.model_json_schema()
    assert set(schema["required"]) == {"overall_assessment", "interventions"}
    assert schema["additionalProperties"] is False
    interventions = schema["properties"]["interventions"]
    assert interventions["maxItems"] == 6
    intervention = schema["$defs"][interventions["items"]["$ref"].rsplit("/", 1)[1]]
    assert intervention["additionalProperties"] is False
    assert set(intervention["required"]) == {"category", "content"}
    assert intervention["properties"]["category"]["enum"] == CATEGORIES
    for block_ref, limit in (
        (schema["properties"]["overall_assessment"], 800),
        (intervention["properties"]["content"], 500),
    ):
        block = schema["$defs"][block_ref["$ref"].rsplit("/", 1)[1]]
        assert set(block["required"]) == {"text", "references"}
        assert block["additionalProperties"] is False
        assert block["properties"]["text"]["type"] == "string"
        assert block["properties"]["text"]["minLength"] == 1
        assert block["properties"]["text"]["maxLength"] == limit
        assert block["properties"]["references"]["type"] == "array"
        assert block["properties"]["references"]["items"] == {"type": "string"}
    user_content = captured["messages"][1]["content"]
    for private in ("private-measurement-id", "private-user-id", "2026-09-20", "关键依据"):
        assert private not in user_content
    data = json.loads(user_content.split("\n", 1)[1])
    assert "measurement_id" not in data and "measured_at" not in data
    weight = data["body_composition"]["weight_kg"]
    assert weight["unit"] == "kg"
    assert weight["reference_id"] == metric_id(make_request(), "body_composition", "weight_kg")
    assert weight["reference_label"] == "体重"
    assert data["body_composition"]["muscle_mass_kg"]["level"] == "low"
    assert data["assessment"]["body_type"]["name"] == "浮肿肥胖型"
    assert "reference_id" not in data["assessment"]["body_type"]


def test_prompt_and_document_define_retest_conditions_consistently():
    from pathlib import Path

    from weight_agent.report.qwen import LITE_SYSTEM_PROMPT, PROMPT_VERSION, SYSTEM_PROMPT

    document = (Path(__file__).resolve().parents[1] / "docs" / "report-prompt.md").read_text(
        encoding="utf-8"
    )
    assert f"版本：`{PROMPT_VERSION}`" in document
    assert "只输出 JSON" in SYSTEM_PROMPT
    assert '"overall_assessment"' in SYSTEM_PROMPT
    assert '"interventions"' in SYSTEM_PROMPT
    for rule in (
        "有身体实测值时输出 retest",
        "同一设备、相近时段和相近身体状态复测",
        "不编造复测周期",
        "interventions 返回 []",
        "overall_assessment 用 2-4 句",
        "运动类型、动作示例和执行要点",
        "body_status 必须解释",
    ):
        assert rule in LITE_SYSTEM_PROMPT
    assert "references" not in LITE_SYSTEM_PROMPT
    assert "key_evidence" in LITE_SYSTEM_PROMPT


@pytest.mark.anyio
async def test_detailed_analysis_and_targeted_training_are_preserved():
    request = make_request()
    summary = (
        "肌肉量偏低，当前应优先改善瘦组织储备。"
        "脂肪控制量提示减脂方向，需要兼顾肌肉保留。"
        "体重与BMI不能单独反映体成分问题。"
    )
    training = (
        "结合肌肉量偏低和减脂目标，可将抗阻训练与有氧活动搭配。"
        "抗阻训练可选择弹力带划船、坐站练习，重视动作控制与全身肌群参与。"
        "有氧活动可选择快走或骑行，按自身耐受调整。"
        "从能够稳定完成的动作和负荷开始，循序渐进，避免为了减重忽视肌肉保留。"
    )
    output = {
        "overall_assessment": referenced_text(summary),
        "interventions": [
            {
                "category": "training",
                "content": referenced_text(
                    training, [metric_id(request, "body_composition", "muscle_mass_kg")]
                ),
            }
        ],
    }
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    assert response.overall_assessment == summary
    result = next(item for item in response.interventions if item.category == "training")
    assert result.content == training + "参考测量：肌肉量 44.7kg。"


@pytest.mark.anyio
@pytest.mark.parametrize("with_references", [False, True])
@pytest.mark.parametrize("partial", [False, True])
async def test_model_retest_is_preserved_without_history(partial, with_references):
    request = (
        ReportRequest.model_validate({"body_composition": {"muscle_mass_kg": 44.7}})
        if partial
        else make_request()
    )
    text = "使用同一设备，在相近时段及相近身体状态下复测，跟踪肌肉量。"
    references = (
        [metric_id(request, "body_composition", "muscle_mass_kg")] if with_references else []
    )
    output = {
        "overall_assessment": referenced_text("现有数据不足以完整判断身体成分状态。"),
        "interventions": [{"category": "retest", "content": referenced_text(text, references)}],
    }
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    fact = "参考测量：肌肉量 44.7。" if partial else "参考测量：肌肉量 44.7kg。"
    expected = text + (fact if with_references else "")
    retest = next(item for item in response.interventions if item.category == "retest")
    assert retest.content == expected


@pytest.mark.anyio
@pytest.mark.parametrize("text", ["建议 4 周后复测。", "建议四周后复测。", "建议每月复测两次。"])
async def test_model_retest_with_numeric_schedule_drops_only_that_block(text):
    output = {
        "overall_assessment": referenced_text("肌肉量偏低。"),
        "interventions": [{"category": "retest", "content": referenced_text(text)}],
    }
    response = await run_model_output(make_request(), output)
    assert response.generation_mode == "model"
    assert response.overall_assessment == "肌肉量偏低。"
    retest = next(item for item in response.interventions if item.category == "retest")
    assert retest.content == fallback_content(make_request(), "retest")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"subject": {"age_years": 28}},
        {"subject": {"sex": "male"}},
        {"exercise_calories_kcal_per_30_min": {"walking": 124}},
        {"body_composition": {"muscle_mass_kg": {"level": "low"}}},
    ],
)
async def test_empty_model_interventions_remain_supported_without_measurements(data):
    text = "缺少身体实测数据，无法判断身体成分状态。"
    response = await run_model_output(ReportRequest.model_validate(data), model_output(text))
    assert response.generation_mode == "model"
    assert response.overall_assessment == text
    assert next(item.content for item in response.interventions if item.category == "retest") == (
        "建议 4 周后复测，重点观察体脂率、肌肉量及本次干预目标的变化。"
    )


@pytest.mark.anyio
async def test_missing_model_retest_still_gets_category_fallback():
    text = "循序渐进进行抗阻训练。"
    output = {
        "overall_assessment": referenced_text("肌肉量偏低。"),
        "interventions": [{"category": "training", "content": referenced_text(text)}],
    }
    response = await run_model_output(make_request(), output)
    assert response.generation_mode == "model"
    training = next(item for item in response.interventions if item.category == "training")
    assert training.content == text
    assert next(item.content for item in response.interventions if item.category == "retest") == (
        "建议 4 周后复测，重点观察体脂率、肌肉量及本次干预目标的变化。"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("content", ['{"unexpected": true}', "not json", "", " ", None, {}])
async def test_qwen_analyzer_rejects_invalid_model_output(content) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

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
    analysis = _ModelAnalysis(
        overall_assessment=referenced_text(
            "体重与BMI应综合观察。",
            [
                metric_id(request, "body_composition", "weight_kg"),
                metric_id(request, "assessment", "bmi"),
            ],
        ),
        interventions=[
            {
                "category": "direction",
                "content": referenced_text(
                    "建议循序渐进减脂。", [metric_id(request, "assessment", "fat_control_kg")]
                ),
            }
        ],
    )
    original = analysis.model_dump()
    original_request = request.model_dump()
    result = resolve_metric_references(request, analysis)
    assert isinstance(result, ReportAnalysis)
    assert result.overall_assessment == "体重与BMI应综合观察。参考测量：体重 62.3kg；BMI 21.1。"
    assert result.interventions[0].content == "建议循序渐进减脂。参考测量：脂肪控制量 -4.5。"
    assert analysis.model_dump() == original
    assert request.model_dump() == original_request


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
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
def test_unverified_numeric_references_are_rejected(location, content) -> None:
    analysis = _ModelAnalysis.model_validate(model_output(content, location=location))
    with pytest.raises(ValueError, match="unverified numeric reference"):
        resolve_metric_references(make_request(), analysis)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("output", "expected_mode"),
    [
        (model_output("体重为120kg。"), "fallback"),
        (model_output("体重 {{body_composition.unknown}}。"), "fallback"),
        (
            {
                "overall_assessment": referenced_text("体重正常。"),
                "interventions": [
                    {"category": "weight", "content": referenced_text("体重120kg。")}
                ],
            },
            "model",
        ),
        (
            {
                "overall_assessment": referenced_text("体重正常。"),
                "interventions": [
                    {"category": "weight", "content": referenced_text("增重")},
                    {"category": "weight", "content": referenced_text("减重")},
                ],
            },
            "fallback",
        ),
    ],
)
async def test_unverified_or_duplicate_model_output_falls_back_or_drops_block(
    output, expected_mode
):
    # 内部 schema 有效，失败必须来自数值校验或公开 schema 的分类唯一性校验。
    _ModelAnalysis.model_validate(output)
    request = make_request()
    response = await run_model_output(request, output)
    assert response.generation_mode == expected_mode
    assert "120" not in response.overall_assessment
    if expected_mode == "model":
        weight = next(item for item in response.interventions if item.category == "weight")
        assert weight.content == fallback_content(request, "weight")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "output",
    [
        {"overall_assessment": "体重正常。", "interventions": []},
        {
            "overall_assessment": referenced_text("体重正常。"),
            "interventions": [{"category": "training", "content": "循序渐进进行抗阻训练。"}],
        },
        {"overall_assessment": {"text": "体重正常。"}, "interventions": []},
        {"overall_assessment": {"references": []}, "interventions": []},
        {"overall_assessment": referenced_text(""), "interventions": []},
        {"overall_assessment": {"text": "体重正常。", "references": None}, "interventions": []},
        {"overall_assessment": {"text": "体重正常。", "references": "weight"}, "interventions": []},
        {"overall_assessment": {"text": "体重正常。", "references": [1]}, "interventions": []},
        {
            "overall_assessment": {**referenced_text("体重正常。"), "value": 120},
            "interventions": [],
        },
        {**model_output(), "key_evidence": []},
        {
            "overall_assessment": referenced_text("体重正常。"),
            "interventions": [{"category": "unknown", "content": referenced_text("注意观察。")}],
        },
    ],
)
async def test_old_protocol_and_invalid_structured_schema_fall_back(output) -> None:
    with pytest.raises(ValidationError):
        _ModelAnalysis.model_validate(output)
    response = await run_model_output(make_request(), output)
    assert response.generation_mode == "fallback"


@pytest.mark.anyio
async def test_model_references_are_rendered_before_returning_report() -> None:
    report_request = make_request()

    def handler(request):
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        assert "reference_id" in prompt and "references" in prompt
        assert "{{字段路径}}" not in prompt
        assert "private-measurement-id" not in payload["messages"][1]["content"]
        assert "2026-09-20" not in payload["messages"][1]["content"]
        sent = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
        return model_response(
            {
                "overall_assessment": referenced_text(
                    "体重正常，肌肉量偏低。",
                    [sent["body_composition"]["weight_kg"]["reference_id"]],
                ),
                "interventions": [
                    {
                        "category": "training",
                        "content": referenced_text(
                            "循序渐进进行抗阻训练。",
                            [sent["body_composition"]["muscle_mass_kg"]["reference_id"]],
                        ),
                    }
                ],
            }
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
            report_request, "anonymous"
        )
    assert response.generation_mode == "model"
    assert response.overall_assessment == "体重正常，肌肉量偏低。参考测量：体重 62.3kg。"
    training = next(item for item in response.interventions if item.category == "training")
    assert training.content == "循序渐进进行抗阻训练。参考测量：肌肉量 44.7kg。"
    assert [item.category for item in response.interventions] == CATEGORIES
    assert "{{" not in response.model_dump_json()
    assert "references" not in response.model_dump_json()
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
    ("field", "label"),
    [
        ("walking", "步行"),
        ("golf", "高尔夫"),
        ("gateball", "门球"),
        ("tennis_cycling_basketball", "网球/自行车/篮球"),
        ("squash_shuttlecock_taekwondo_fencing", "壁球/毽子/跆拳道/击剑"),
        ("climbing", "爬山"),
        ("swimming_aerobics_jogging_football_jumping_rope", "游泳/有氧操/慢跑/足球/跳绳"),
        ("badminton_table_tennis", "羽毛球/乒乓球"),
    ],
)
@pytest.mark.anyio
async def test_all_exercise_references_keep_model_generation(field, label):
    report_request = ReportRequest.model_validate(
        {"exercise_calories_kcal_per_30_min": {field: {"value": 124, "unit": "kCal/30min"}}}
    )
    output = model_output(
        "运动消耗仅供选择运动方向参考。",
        [metric_id(report_request, "exercise_calories_kcal_per_30_min", field)],
    )
    response = await run_model_output(report_request, output)
    assert response.generation_mode == "model"
    assert response.overall_assessment == (
        f"运动消耗仅供选择运动方向参考。参考测量：{label}消耗参考 124kCal/30min。"
    )


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
        "BMI二点一。",
        "体重约为二點一。",
        "建议减脂二成。",
        "每周训练三回。",
        "每次练习三组。",
        "体重 {{assessment.bmi}}，BMI {{body_composition.weight_kg}}。",
        "体重为 {{assessment.bmi}}。",
        "肌肉量 {{body_composition.weight_kg}}。",
        "BMI {{body_composition.muscle_mass_kg}}。",
        "左臂肌肉量 {{segmental_composition.muscle_mass_kg.right_arm}}。",
        "体重 {{body_composition.weight_kg}}kg。",
        "体脂率 {{assessment.body_fat_rate_percent}}%。",
        "体脂率 {{assessment.body_fat_rate_percent}}％。",
        "体重 +{{body_composition.weight_kg}}。",
        "脂肪控制量 -{{assessment.fat_control_kg}}。",
        "脂肪控制量 −{{assessment.fat_control_kg}}。",
        "体重 ＋{{body_composition.weight_kg}}。",
    ],
)
@pytest.mark.anyio
async def test_chinese_numbers_and_obvious_label_mismatches_fall_back_or_drop_block(
    location, content
):
    request = make_request()
    # 即使附带可用编号，也不能让 text 中的旧协议或未经验证的数字通过。
    output = model_output(
        content, [metric_id(request, "body_composition", "weight_kg")], location=location
    )
    analysis = _ModelAnalysis.model_validate(output)
    with pytest.raises(ValueError, match="unverified numeric reference"):
        resolve_metric_references(request, analysis)
    response = await run_model_output(request, output)
    if location == "overall_assessment":
        assert response.generation_mode == "fallback"
        return
    assert response.generation_mode == "model"
    assert response.overall_assessment == "肌肉量偏低。"
    body_status = next(item for item in response.interventions if item.category == "body_status")
    assert body_status.content == fallback_content(request, "body_status")


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize("with_references", [False, True])
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
        "训练计划不应一成不变。",
        "体重与体质指数应综合观察。",
        "肌肉量偏低，应结合体重调整训练。",
        "体重与BMI需要结合身体成分综合判断。",
    ],
)
def test_qualitative_chinese_and_valid_label_references_remain_supported(
    location, with_references, content
):
    request = make_request()
    references = (
        [
            metric_id(request, "body_composition", "weight_kg"),
            metric_id(request, "assessment", "bmi"),
        ]
        if with_references
        else []
    )
    result = resolve_metric_references(
        request,
        _ModelAnalysis.model_validate(model_output(content, references, location=location)),
    )
    rendered = (
        result.overall_assessment
        if location == "overall_assessment"
        else result.interventions[0].content
    )
    assert rendered == content + ("参考测量：体重 62.3kg；BMI 21.1。" if with_references else "")


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.anyio
async def test_segment_names_and_values_are_bound_independently_of_reference_order(
    location, reverse
):
    request = ReportRequest.model_validate(
        {
            "segmental_composition": {
                "muscle_mass_kg": {
                    "right_arm": {"value": "3.10", "unit": "kg"},
                    "left_arm": {"value": "2.20", "unit": "kg"},
                }
            }
        }
    )
    keys = ["left_arm", "right_arm"]
    facts = ["左臂肌肉量 2.2kg", "右臂肌肉量 3.1kg"]
    if reverse:
        keys.reverse()
        facts.reverse()
    output = model_output(
        "左臂与右臂肌肉量需要综合观察。",
        [metric_id(request, "segmental_composition", "muscle_mass_kg", key) for key in keys],
        location=location,
    )
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    rendered = (
        response.overall_assessment
        if location == "overall_assessment"
        else next(item.content for item in response.interventions if item.category == "body_status")
    )
    assert rendered == "左臂与右臂肌肉量需要综合观察。参考测量：" + "；".join(facts) + "。"


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize("reverse", [False, True])
def test_bmi_and_weight_reference_order_cannot_swap_labels(location, reverse):
    request = make_request()
    references = [
        metric_id(request, "body_composition", "weight_kg"),
        metric_id(request, "assessment", "bmi"),
    ]
    facts = ["体重 62.3kg", "BMI 21.1"]
    if reverse:
        references.reverse()
        facts.reverse()
    result = resolve_metric_references(
        request,
        _ModelAnalysis.model_validate(
            model_output("体重与BMI应结合肌肉量综合观察。", references, location=location)
        ),
    )
    rendered = (
        result.overall_assessment
        if location == "overall_assessment"
        else result.interventions[0].content
    )
    assert rendered == "体重与BMI应结合肌肉量综合观察。参考测量：" + "；".join(facts) + "。"


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize(
    ("text", "paths", "facts"),
    [
        (
            "标准体重需要结合实际身体成分理解。",
            [("assessment", "ideal_body_weight_kg")],
            "理想体重 60kg",
        ),
        (
            "脂肪量与肌肉量应综合观察。",
            [("body_composition", "muscle_mass_kg"), ("body_composition", "body_fat_mass_kg")],
            "肌肉量 44.7kg；脂肪量 14.3kg",
        ),
    ],
)
@pytest.mark.anyio
async def test_qualitative_standard_weight_and_parallel_labels_keep_model_mode(
    location, text, paths, facts
):
    request = ReportRequest.model_validate(
        {
            "body_composition": {
                "body_fat_mass_kg": {"value": "14.3", "unit": "kg"},
                "muscle_mass_kg": {"value": "44.7", "unit": "kg"},
            },
            "assessment": {"ideal_body_weight_kg": {"value": 60, "unit": "kg"}},
        }
    )
    output = model_output(text, [metric_id(request, *path) for path in paths], location=location)
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    rendered = (
        response.overall_assessment
        if location == "overall_assessment"
        else next(item.content for item in response.interventions if item.category == "body_status")
    )
    assert rendered == f"{text}参考测量：{facts}。"


def test_segment_and_extended_impedance_references_remain_supported():
    request = ReportRequest.model_validate(
        {
            "segmental_composition": {"muscle_mass_kg": {"left_arm": {"value": 2, "unit": "kg"}}},
            "bioelectrical_impedance": {"50kHz_left_arm": {"value": 250, "unit": "Ω"}},
            "assessment": {"ideal_body_weight_kg": 60, "weight_control_kg": -2},
        }
    )
    paths = [
        ("segmental_composition", "muscle_mass_kg", "left_arm"),
        ("bioelectrical_impedance", "50kHz_left_arm"),
        ("assessment", "ideal_body_weight_kg"),
        ("assessment", "weight_control_kg"),
    ]
    result = resolve_metric_references(
        request,
        _ModelAnalysis.model_validate(
            model_output(
                "结合节段与体重控制参考综合观察。", [metric_id(request, *path) for path in paths]
            )
        ),
    )
    assert result.overall_assessment == (
        "结合节段与体重控制参考综合观察。参考测量：左臂肌肉量 2kg；"
        "扩展项「50kHz_left_arm」阻抗 250Ω；理想体重 60；体重控制量 -2。"
    )


@pytest.mark.parametrize(
    "name",
    [
        "左臂高频",
        "50kHz_left_arm",
        "50kHz-left-arm",
        "左臂(高频)",
        "左臂（高频）",
        "{left_arm}",
        "{{right_arm}}",
        "50kHz.right_arm",
        "bioelectrical_impedance.right_arm",
        "right_arm.value",
        "right_arm.",
    ],
)
@pytest.mark.anyio
async def test_extended_impedance_names_bind_exactly_without_splitting_keys(name):
    request = ReportRequest.model_validate(
        {
            "bioelectrical_impedance": {
                "right_arm": {"value": "321.50", "unit": "Ω"},
                name: {"value": "250.123456789", "unit": "ohm"},
            }
        }
    )
    data, catalog = _reference_data(request)
    regular = data["bioelectrical_impedance"]["right_arm"]
    extended = data["bioelectrical_impedance"][name]
    assert regular["reference_id"] != extended["reference_id"]
    assert regular["reference_label"] == "右臂阻抗"
    assert extended["reference_label"] == f"扩展项「{name}」阻抗"
    assert catalog[extended["reference_id"]]["value"] == Decimal("250.123456789")
    output = model_output(
        "阻抗仅作身体成分参考。", [extended["reference_id"], regular["reference_id"]]
    )
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    assert response.overall_assessment == (
        f"阻抗仅作身体成分参考。参考测量：扩展项「{name}」阻抗 250.123456789ohm；右臂阻抗 321.5Ω。"
    )


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("label", "BMI"), ("reference_label", "BMI"), ("value", "120"), ("unit", "lb")],
)
@pytest.mark.anyio
async def test_reference_objects_cannot_override_backend_facts(location, field, value):
    request = make_request()
    output = model_output(location=location)
    block = (
        output["overall_assessment"]
        if location == "overall_assessment"
        else output["interventions"][0]["content"]
    )
    block["references"] = [
        {"reference_id": metric_id(request, "body_composition", "weight_kg"), field: value}
    ]
    with pytest.raises(ValidationError) as exc:
        _ModelAnalysis.model_validate(output)
    assert exc.value.errors()[0]["loc"][-2:] == ("references", 0)
    response = await run_model_output(request, output)
    assert response.generation_mode == "fallback"


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize(
    "kind",
    ["duplicate", "unknown", "field_path", "value_path", "body_type", "identity", "date"],
)
@pytest.mark.anyio
async def test_duplicate_unknown_and_non_id_references_are_rejected(location, kind):
    request = make_request()
    known_id = metric_id(request, "body_composition", "weight_kg")
    references = {
        "duplicate": [known_id, known_id],
        "unknown": [known_id, "unavailable-reference"],
        "field_path": ["body_composition.weight_kg"],
        "value_path": ["body_composition.weight_kg.value"],
        "body_type": ["assessment.body_type"],
        "identity": ["measurement_id"],
        "date": ["measured_at.year"],
    }[kind]
    output = model_output("体重应综合观察。", references, location=location)
    with pytest.raises(ValueError, match="unique|unavailable metric"):
        resolve_metric_references(request, _ModelAnalysis.model_validate(output))
    response = await run_model_output(request, output)
    if location == "overall_assessment":
        assert response.generation_mode == "fallback"
        return
    assert response.generation_mode == "model"
    assert response.overall_assessment == "肌肉量偏低。"
    body_status = next(item for item in response.interventions if item.category == "body_status")
    assert body_status.content == fallback_content(request, "body_status")


def test_missing_values_and_zero_intake_have_no_reference_ids():
    request = ReportRequest.model_validate(
        {
            "subject": {"age_years": {"value": None, "unit": "岁"}},
            "body_composition": {"weight_kg": {"unit": "kg", "level": "low", "standard_min": 55}},
            "bioelectrical_impedance": {"缺值.右臂": {"value": None, "unit": "Ω"}},
            "assessment": {
                "recommended_calorie_intake_kcal": {"value": 0, "unit": "kCal"},
                "weight_control_kg": {"value": 0, "unit": "kg"},
                "body_type": {"code": 4, "name": "浮肿肥胖型"},
            },
            "segment_standards": {"muscle": {"right_arm": 0}},
        }
    )
    data, catalog = _reference_data(request)
    unavailable = [
        data["subject"]["age_years"],
        data["body_composition"]["weight_kg"],
        data["bioelectrical_impedance"]["缺值.右臂"],
        data["assessment"]["bmi"],
        data["assessment"]["recommended_calorie_intake_kcal"],
        data["assessment"]["body_type"],
        data["segment_standards"]["muscle"],
    ]
    for metric in unavailable:
        assert "reference_id" not in metric
        assert "reference_label" not in metric
    control = data["assessment"]["weight_control_kg"]
    assert catalog == {control["reference_id"]: control}
    result = resolve_metric_references(
        request,
        _ModelAnalysis.model_validate(model_output("无需调整体重。", [control["reference_id"]])),
    )
    assert result.overall_assessment == "无需调整体重。参考测量：体重控制量 0kg。"


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize("value", [None, 0])
@pytest.mark.anyio
async def test_model_zero_intake_reference_uses_safe_fallback(location, value):
    available = ReportRequest.model_validate(
        {"assessment": {"recommended_calorie_intake_kcal": {"value": 1800, "unit": "kCal"}}}
    )
    reference = metric_id(available, "assessment", "recommended_calorie_intake_kcal")
    report_request = ReportRequest.model_validate(
        {"assessment": {"recommended_calorie_intake_kcal": {"value": value, "unit": "kCal"}}}
    )
    data, catalog = _reference_data(report_request)
    assert "reference_id" not in data["assessment"]["recommended_calorie_intake_kcal"]
    assert catalog == {}
    output = model_output("请关注营养摄入。", [reference], location=location)
    if location == "intervention":
        output["interventions"][0]["category"] = "nutrition"
    with pytest.raises(ValueError, match="unavailable metric"):
        resolve_metric_references(report_request, _ModelAnalysis.model_validate(output))
    response = await run_model_output(report_request, output)
    nutrition = next(
        item.content for item in response.interventions if item.category == "nutrition"
    )
    if location == "overall_assessment":
        assert response.generation_mode == "fallback"
    else:
        assert response.generation_mode == "model"
        assert response.overall_assessment == "肌肉量偏低。"
    assert "0kCal" not in nutrition
    assert "营养参考数据" in nutrition


@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.anyio
async def test_missing_measurement_cannot_be_referenced_from_standards(location):
    available = ReportRequest.model_validate({"body_composition": {"weight_kg": 62.3}})
    reference = metric_id(available, "body_composition", "weight_kg")
    request = ReportRequest.model_validate(
        {"body_composition": {"weight_kg": {"unit": "kg", "level": "normal", "standard_min": 55}}}
    )
    assert _reference_data(request)[1] == {}
    output = model_output("体重应综合观察。", [reference], location=location)
    with pytest.raises(ValueError, match="unavailable metric"):
        resolve_metric_references(request, _ModelAnalysis.model_validate(output))
    response = await run_model_output(request, output)
    if location == "overall_assessment":
        assert response.generation_mode == "fallback"
        return
    assert response.generation_mode == "model"
    assert response.overall_assessment == "肌肉量偏低。"
    body_status = next(item for item in response.interventions if item.category == "body_status")
    assert body_status.content == fallback_content(request, "body_status")


@pytest.mark.parametrize(
    ("location", "limit"), [("overall_assessment", 800), ("intervention", 500)]
)
@pytest.mark.parametrize("excess", [0, 1])
@pytest.mark.parametrize("with_references", [False, True])
@pytest.mark.anyio
async def test_final_rendered_length_limits(location, limit, excess, with_references):
    request = make_request()
    references = [metric_id(request, "body_composition", "weight_kg")] if with_references else []
    suffix = "参考测量：体重 62.3kg。" if with_references else ""
    text = ("观察" * limit)[: limit + excess - len(suffix) - 1] + "。"
    expected = text + suffix
    assert len(expected) == limit + excess
    output = model_output(text, references, location=location)
    if with_references:
        # 正文本身有效，超限必须发生在后端追加参考句之后。
        _ModelAnalysis.model_validate(output)
    if excess:
        with pytest.raises(ValidationError):
            resolve_metric_references(request, _ModelAnalysis.model_validate(output))
    else:
        result = resolve_metric_references(request, _ModelAnalysis.model_validate(output))
        rendered = (
            result.overall_assessment
            if location == "overall_assessment"
            else result.interventions[0].content
        )
        assert rendered == expected
    response = await run_model_output(request, output)
    if excess and with_references and location == "intervention":
        # 正文本身有效，超限只发生在后端追加参考句之后：仅丢弃该条，其余按缺类补齐。
        assert response.generation_mode == "model"
        assert response.overall_assessment == "肌肉量偏低。"
        body_status = next(
            item for item in response.interventions if item.category == "body_status"
        )
        assert body_status.content == fallback_content(request, "body_status")
    else:
        assert response.generation_mode == ("fallback" if excess else "model")


@pytest.mark.parametrize("ending", ["", "。", "！", "？", ".", "!", "?"])
def test_references_are_appended_as_a_separate_sentence(ending):
    request = make_request()
    text = "注意观察" + ending
    result = resolve_metric_references(
        request,
        _ModelAnalysis.model_validate(
            model_output(text, [metric_id(request, "body_composition", "weight_kg")])
        ),
    )
    assert result.overall_assessment == text + ("" if ending else "。") + "参考测量：体重 62.3kg。"


@pytest.mark.parametrize(
    ("ratio", "rendered_ratio"),
    [("0.123456789012", "0.123456789012"), ("1E-12", "0.000000000001")],
)
def test_build_payload_preserves_exact_decimal_strings_and_reference_catalog(ratio, rendered_ratio):
    request = ReportRequest.model_validate(
        {
            "body_composition": {
                "weight_kg": {
                    "value": Decimal("62.3123456789"),
                    "unit": "kg",
                    "standard_min": Decimal("55.3123456789"),
                    "standard_max": Decimal("74.9123456789"),
                }
            },
            "assessment": {
                "waist_hip_ratio": {"value": Decimal(ratio)},
                "fat_control_kg": {"value": Decimal("-4.5123456789"), "unit": "kg"},
                "recommended_calorie_intake_kcal": {"value": 0, "unit": "kCal"},
            },
        }
    )
    original = request.model_dump()
    analyzer = QwenReportAnalyzer(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="mock",
        request_timeout_seconds=1,
    )
    payload = analyzer._build_payload(request)
    sent = json.loads(payload["messages"][1]["content"].split("\n", 1)[1])
    data, catalog = _reference_data(request)
    assert sent == json.loads(json.dumps(data, default=str))
    weight = sent["body_composition"]["weight_kg"]
    assert weight["value"] == "62.3123456789"
    assert weight["standard_min"] == "55.3123456789"
    assert weight["standard_max"] == "74.9123456789"
    assert sent["assessment"]["waist_hip_ratio"]["value"] == ratio
    assert sent["assessment"]["fat_control_kg"]["value"] == "-4.5123456789"
    assert "reference_id" not in sent["assessment"]["recommended_calorie_intake_kcal"]
    assert "reference_id" not in sent["body_composition"]["muscle_mass_kg"]
    assert len(catalog) == 3
    assert all(re.fullmatch(r"m\d+", reference) for reference in catalog)
    assert all(isinstance(metric["value"], Decimal) for metric in catalog.values())
    result = resolve_metric_references(
        request,
        _ModelAnalysis.model_validate(
            model_output(
                "结合体重与身体成分综合观察。",
                [
                    weight["reference_id"],
                    sent["assessment"]["waist_hip_ratio"]["reference_id"],
                    sent["assessment"]["fat_control_kg"]["reference_id"],
                ],
            )
        ),
    )
    assert result.overall_assessment == (
        "结合体重与身体成分综合观察。参考测量：体重 62.3123456789kg；"
        f"腰臀比 {rendered_ratio}；脂肪控制量 -4.5123456789kg。"
    )
    assert request.model_dump() == original


@pytest.mark.anyio
async def test_complete_sample_http_to_mock_model_and_all_metric_references(monkeypatch):
    import runpy
    from pathlib import Path

    from weight_agent.core import config

    # main 的模块级 app 也必须使用显式配置，首次导入时不读取 .env。
    settings = config.Settings(_env_file=None, environment="test", dashscope_api_key=None)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    from weight_agent.main import create_app

    script = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_report_models.py"
    report_request = runpy.run_path(str(script), run_name="sample_test")["sample_request"]()
    data, catalog = _reference_data(report_request)
    pending = [((), report_request.model_dump())]
    found_ids = set()
    while pending:
        path, value = pending.pop()
        if not isinstance(value, dict):
            continue
        if value.get("value") is not None:
            node = data
            for key in path:
                node = node[key]
            reference = node["reference_id"]
            assert reference not in found_ids, path
            assert re.fullmatch(r"m\d+", reference), path
            assert catalog[reference] == node
            assert node["value"] == value["value"]
            resolved = resolve_metric_references(
                report_request,
                _ModelAnalysis.model_validate(model_output("指标仅供综合参考。", [reference])),
            )
            number = format(value["value"], "f")
            if "." in number:
                number = number.rstrip("0").rstrip(".")
            expected_fact = f"{node['reference_label']} {number}{value.get('unit') or ''}"
            assert (
                resolved.overall_assessment == f"指标仅供综合参考。参考测量：{expected_fact}。"
            ), path
            found_ids.add(reference)
        else:
            pending.extend(((*path, key), item) for key, item in value.items())
    assert len(found_ids) > 50
    assert found_ids == set(catalog)

    calls = []

    def handler(request):
        calls.append(request)
        sent = json.loads(request.content)["messages"][1]["content"]
        assert "private-user" not in sent
        assert "measurement_id" not in sent and "measured_at" not in sent
        metrics = json.loads(sent.split("\n", 1)[1])
        output = {
            "overall_assessment": referenced_text(
                "体重与BMI需综合观察，运动消耗仅供参考。",
                [
                    metrics["assessment"]["bmi"]["reference_id"],
                    metrics["body_composition"]["weight_kg"]["reference_id"],
                    metrics["exercise_calories_kcal_per_30_min"]["walking"]["reference_id"],
                ],
            ),
            "interventions": [
                {"category": "training", "content": referenced_text("循序渐进进行抗阻训练。")},
                {
                    "category": "retest",
                    "content": referenced_text(
                        "使用同一设备，在相近时段及相近身体状态下复测，跟踪体脂率和肌肉量。"
                    ),
                },
            ],
        }
        return model_response(output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as model_client:
        app = create_app(
            settings,
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
    assert len(calls) == 1
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
    assert result["overall_assessment"] == (
        "体重与BMI需综合观察，运动消耗仅供参考。参考测量："
        "BMI 21.1kg/m2；体重 62.3kg；步行消耗参考 124kCal/30min。"
    )
    assert [item["category"] for item in result["interventions"]] == CATEGORIES
    assert result["interventions"][1]["content"] == "循序渐进进行抗阻训练。"
    assert result["interventions"][4]["content"] == (
        "使用同一设备，在相近时段及相近身体状态下复测，跟踪体脂率和肌肉量。"
    )
    assert all(isinstance(item["content"], str) for item in result["interventions"])
    assert result["key_evidence"] == []
    assert "{{" not in response.text and "references" not in response.text
    assert float(response.headers["X-Response-Time-Ms"]) >= 0


_DIAGNOSTIC_LOGGER = "weight_agent.report.qwen"
_PRIVATE_TEXT = "私密健康正文：体态需综合观察。"
_PRIVATE_TOKEN = "private-patient-secret"


def assert_failure_diagnostic(
    caplog,
    *,
    stage,
    reason,
    field,
    validation_type="none",
    finish_reason="unknown",
    status_code=200,
    private=(),
):
    # 只约束报告诊断 logger，不对 httpx 自己记录的请求 URL 作断言。
    records = [record for record in caplog.records if record.name == _DIAGNOSTIC_LOGGER]
    warnings = [record for record in records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.levelno == logging.WARNING
    assert warning.getMessage().startswith("report_model_failed ")
    expected = {
        "failure_stage": stage,
        "failure_reason": reason,
        "failure_field": field,
        "validation_type": validation_type,
        "finish_reason": finish_reason,
        "status_code": status_code,
    }
    assert {key: getattr(warning, key, "<missing>") for key in expected} == expected
    for key, value in expected.items():
        label = key.removeprefix("failure_")
        assert f"{label}={value if value is not None else 'none'}" in warning.getMessage().split()

    standard_keys = logging.makeLogRecord({}).__dict__.keys()
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        extra = {key: value for key, value in vars(record).items() if key not in standard_keys}
        logged = repr((record.msg, record.args, extra, record.getMessage()))
        for secret in (
            _PRIVATE_TEXT,
            "私密健康正文",
            _PRIVATE_TOKEN,
            "private-measurement-id",
            "private-user-id",
            "test-key",
            "2026-09-20",
            "62.3",
            "44.7",
            "浮肿肥胖型",
            "肌肉量偏低。",
            *private,
        ):
            escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
            assert secret not in logged
            assert repr(secret)[1:-1] not in logged
            assert escaped not in logged
            assert repr(escaped)[1:-1] not in logged


def assert_drop_diagnostics(caplog, drops, *, private=()):
    # 只约束报告诊断 logger，不对 httpx 自己记录的请求 URL 作断言。
    records = [record for record in caplog.records if record.name == _DIAGNOSTIC_LOGGER]
    warnings = [record for record in records if record.levelno >= logging.WARNING]
    assert len(warnings) == len(drops)
    for warning, expected in zip(warnings, drops, strict=True):
        assert warning.levelno == logging.WARNING
        assert warning.getMessage().startswith("report_intervention_dropped ")
        assert getattr(warning, "failure_stage", "<missing>") == "resolve_references"
        assert getattr(warning, "failure_reason", "<missing>") == expected["reason"]
        assert getattr(warning, "failure_field", "<missing>") == expected["field"]
        assert getattr(warning, "validation_type", "<missing>") == expected["validation_type"]
        tokens = warning.getMessage().split()
        assert "stage=resolve_references" in tokens
        assert f"reason={expected['reason']}" in tokens
        assert f"field={expected['field']}" in tokens
        assert f"validation_type={expected['validation_type']}" in tokens

    standard_keys = logging.makeLogRecord({}).__dict__.keys()
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        extra = {key: value for key, value in vars(record).items() if key not in standard_keys}
        logged = repr((record.msg, record.args, extra, record.getMessage()))
        for secret in (
            _PRIVATE_TEXT,
            "私密健康正文",
            _PRIVATE_TOKEN,
            "private-measurement-id",
            "private-user-id",
            "test-key",
            "2026-09-20",
            "62.3",
            "44.7",
            "浮肿肥胖型",
            "肌肉量偏低。",
            *private,
        ):
            escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
            assert secret not in logged
            assert repr(secret)[1:-1] not in logged
            assert escaped not in logged
            assert repr(escaped)[1:-1] not in logged


def assert_whole_report_fallback(response, request):
    from weight_agent.report.workflow import build_fallback_analysis

    expected = build_fallback_analysis(request)
    assert response.generation_mode == "fallback"
    assert response.overall_assessment == expected.overall_assessment
    assert [item.category for item in response.interventions] == CATEGORIES
    assert {item.category: item.content for item in response.interventions} == {
        item.category: item.content for item in expected.interventions
    }
    assert response.key_evidence == []


async def run_diagnostic_transport(handler, *, workflow=True):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        analyzer = QwenReportAnalyzer(
            api_key="test-key",
            base_url=f"https://example.invalid/{_PRIVATE_TOKEN}",
            model="mock",
            request_timeout_seconds=1,
            client=client,
        )
        if not workflow:
            return await analyzer.analyze(make_request(), "private-user-id")
        return await ReportWorkflow(analyzer, timeout_seconds=1).run(
            make_request(), "private-user-id"
        )


@pytest.mark.anyio
@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{占位}", "forbidden_symbol"),
        ("%", "forbidden_symbol"),
        ("+", "forbidden_symbol"),
        ("-", "forbidden_symbol"),
        ("体重62.3kg。", "literal_number"),
        ("建议每次练习三组。", "literal_number"),
    ],
)
async def test_diagnostic_text_failure_keeps_value_error_and_drops_block_or_falls_back(
    caplog, location, text, reason
):
    request = make_request()
    output = model_output(_PRIVATE_TEXT + text, location=location)
    output["interventions"].append(
        {"category": "training", "content": referenced_text(_PRIVATE_TEXT)}
    )
    with pytest.raises(ValueError, match="unverified numeric reference"):
        resolve_metric_references(request, _ModelAnalysis.model_validate(output))
    response = await run_model_output(request, output)
    if location == "intervention":
        # 单条建议校验失败时仅丢弃该条，其余模型建议保留，缺类按规则补齐。
        assert response.generation_mode == "model"
        assert response.overall_assessment == "肌肉量偏低。"
        training = next(item for item in response.interventions if item.category == "training")
        assert training.content == _PRIVATE_TEXT
        body_status = next(
            item for item in response.interventions if item.category == "body_status"
        )
        assert body_status.content == fallback_content(request, "body_status")
        assert_drop_diagnostics(
            caplog,
            [
                {
                    "reason": reason,
                    "field": "interventions[0].content.text",
                    "validation_type": "none",
                }
            ],
        )
        return
    assert_whole_report_fallback(response, request)
    assert_failure_diagnostic(
        caplog, stage="resolve_references", reason=reason, field="overall_assessment.text"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_diagnostic_reference_failure_does_not_log_reference_ids(caplog, location, duplicate):
    request = make_request()
    reference = metric_id(request, "body_composition", "weight_kg")
    references = [reference, reference] if duplicate else [_PRIVATE_TOKEN]
    output = model_output(_PRIVATE_TEXT, references, location=location)
    response = await run_model_output(request, output)
    field = (
        "overall_assessment.references"
        if location == "overall_assessment"
        else ("interventions[0].content.references")
    )
    reason = "duplicate_reference" if duplicate else "unknown_reference"
    expected_field = field if duplicate else f"{field}[0]"
    if location == "intervention":
        # 单条建议校验失败时仅丢弃该条，其余模型建议保留，缺类按规则补齐。
        assert response.generation_mode == "model"
        assert response.overall_assessment == "肌肉量偏低。"
        body_status = next(
            item for item in response.interventions if item.category == "body_status"
        )
        assert body_status.content == fallback_content(request, "body_status")
        assert_drop_diagnostics(
            caplog,
            [{"reason": reason, "field": expected_field, "validation_type": "none"}],
            private=references,
        )
        return
    assert_whole_report_fallback(response, request)
    assert_failure_diagnostic(
        caplog,
        stage="resolve_references",
        reason=reason,
        field=expected_field,
        private=references,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("output", "field", "validation_type"),
    [
        (
            {**model_output(_PRIVATE_TEXT), _PRIVATE_TOKEN: _PRIVATE_TEXT},
            "<extra>",
            "extra_forbidden",
        ),
        (
            {
                "overall_assessment": {**referenced_text(_PRIVATE_TEXT), _PRIVATE_TOKEN: True},
                "interventions": [],
            },
            "overall_assessment.<extra>",
            "extra_forbidden",
        ),
        (
            {
                "overall_assessment": referenced_text(_PRIVATE_TEXT),
                "interventions": [
                    {
                        "category": "training",
                        "content": {**referenced_text(_PRIVATE_TEXT), _PRIVATE_TOKEN: True},
                    }
                ],
            },
            "interventions[0].content.<extra>",
            "extra_forbidden",
        ),
        (
            {
                "overall_assessment": referenced_text(_PRIVATE_TEXT),
                "interventions": [
                    {"category": _PRIVATE_TOKEN, "content": referenced_text(_PRIVATE_TEXT)}
                ],
            },
            "interventions[0].category",
            "literal_error",
        ),
        (
            {
                "overall_assessment": referenced_text(_PRIVATE_TEXT),
                "interventions": [
                    {
                        "category": "training",
                        "content": {"text": _PRIVATE_TEXT, "references": [{_PRIVATE_TOKEN: True}]},
                    }
                ],
            },
            "interventions[0].content.references[0]",
            "string_type",
        ),
        (
            {"overall_assessment": {"references": []}, "interventions": []},
            "overall_assessment.text",
            "missing",
        ),
        ([], "$", "model_type"),
    ],
    ids=[
        "root-extra",
        "summary-extra",
        "content-extra",
        "category",
        "reference",
        "missing",
        "root",
    ],
)
async def test_diagnostic_schema_locations_are_allowlisted(caplog, output, field, validation_type):
    response = await run_model_output(make_request(), output)
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog,
        stage="model_schema",
        reason="schema_validation",
        field=field,
        validation_type=validation_type,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("location", "limit"), [("overall_assessment", 800), ("intervention", 500)]
)
@pytest.mark.parametrize("rendered", [False, True])
async def test_diagnostic_distinguishes_internal_and_rendered_length(
    caplog, location, limit, rendered
):
    request = make_request()
    references = [metric_id(request, "body_composition", "weight_kg")] if rendered else []
    text = _PRIVATE_TEXT + "观" * (limit - len(_PRIVATE_TEXT) + (not rendered))
    output = model_output(text, references, location=location)
    if rendered:
        _ModelAnalysis.model_validate(output)
    response = await run_model_output(request, output)
    field = "overall_assessment" if location == "overall_assessment" else "interventions[0].content"
    if rendered and location == "intervention":
        # 正文本身有效，超限只发生在后端追加参考句之后：仅丢弃该条，其余按缺类补齐。
        assert response.generation_mode == "model"
        assert response.overall_assessment == "肌肉量偏低。"
        body_status = next(
            item for item in response.interventions if item.category == "body_status"
        )
        assert body_status.content == fallback_content(request, "body_status")
        assert_drop_diagnostics(
            caplog,
            [
                {
                    "reason": "rendered_text_too_long",
                    "field": field,
                    "validation_type": "string_too_long",
                }
            ],
        )
        return
    assert_whole_report_fallback(response, request)
    assert_failure_diagnostic(
        caplog,
        stage="resolve_references" if rendered else "model_schema",
        reason="rendered_text_too_long" if rendered else "text_too_long",
        field=field if rendered else f"{field}.text",
        validation_type="string_too_long",
    )


@pytest.mark.anyio
async def test_diagnostic_duplicate_category_is_public_schema_failure(caplog):
    output = model_output(_PRIVATE_TEXT, location="intervention")
    output["interventions"].append(output["interventions"][0].copy())
    analysis = _ModelAnalysis.model_validate(output)
    with pytest.raises(ValidationError, match="categories must be unique"):
        resolve_metric_references(make_request(), analysis)
    response = await run_model_output(make_request(), output)
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog,
        stage="resolve_references",
        reason="duplicate_category",
        field="interventions",
        validation_type="value_error",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("location", ["overall_assessment", "intervention"])
async def test_diagnostic_invalid_text_encoding_is_resolver_failure(caplog, monkeypatch, location):
    analysis = _ModelAnalysis.model_validate(model_output(_PRIVATE_TEXT, location=location))
    block = (
        analysis.overall_assessment
        if location == "overall_assessment"
        else analysis.interventions[0].content
    )
    # Pydantic 和 JSON 解码器均先拒绝孤立代理项；构造后注入以覆盖正文 UTF-8 校验。
    block.text += "\ud800"
    monkeypatch.setattr(_ModelAnalysis, "model_validate_json", lambda *args, **kwargs: analysis)
    with pytest.raises(ValueError):
        resolve_metric_references(make_request(), analysis)
    response = await run_model_output(make_request(), model_output(_PRIVATE_TEXT))
    # 孤立代理项是在本地直接构造的异常，无法通过合法 JSON 从模型响应传入；
    # 上面的 resolver 测试已覆盖该分支，正常 HTTP JSON 响应应保持模型模式。
    assert response.generation_mode == "model"
    assert not [
        record
        for record in caplog.records
        if record.name == _DIAGNOSTIC_LOGGER and record.levelno >= logging.WARNING
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("", "empty_content"),
        (" \n\t", "empty_content"),
        (None, "empty_content"),
        ({_PRIVATE_TOKEN: _PRIVATE_TEXT}, "invalid_content_type"),
        ([_PRIVATE_TEXT], "invalid_content_type"),
        (True, "invalid_content_type"),
    ],
)
async def test_diagnostic_response_content_failure(caplog, content, reason):
    response = await run_diagnostic_transport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog, stage="response_content", reason=reason, field="choices[0].message.content"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": {_PRIVATE_TOKEN: _PRIVATE_TEXT}},
        {"choices": [None]},
        {"choices": [{"message": []}]},
        {"choices": [{"message": {}}]},
    ],
)
async def test_diagnostic_invalid_response_structure(caplog, body):
    response = await run_diagnostic_transport(lambda request: httpx.Response(200, json=body))
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog,
        stage="response_content",
        reason="invalid_response_structure",
        field="choices[0].message.content",
    )


@pytest.mark.anyio
async def test_diagnostic_invalid_http_json_does_not_log_response_body(caplog):
    response = await run_diagnostic_transport(
        lambda request: httpx.Response(200, text=f"<html>{_PRIVATE_TEXT}{_PRIVATE_TOKEN}</html>")
    )
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog, stage="response_json", reason="invalid_response_json", field="response"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("finish", "expected"),
    [
        ({"finish_reason": value}, value)
        for value in ("stop", "length", "content_filter", "tool_calls", "function_call")
    ]
    + [
        ({}, "unknown"),
        ({"finish_reason": None}, "unknown"),
        ({"finish_reason": _PRIVATE_TOKEN + "\nforged-warning"}, "unknown"),
        ({"finish_reason": [_PRIVATE_TOKEN]}, "unknown"),
    ],
)
async def test_diagnostic_invalid_model_json_sanitizes_finish_reason(caplog, finish, expected):
    content = '{"overall_assessment":{"text":"' + _PRIVATE_TEXT + _PRIVATE_TOKEN
    response = await run_diagnostic_transport(
        lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": content}, **finish}]}
        )
    )
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog,
        stage="model_schema",
        reason="invalid_model_json",
        field="$",
        validation_type="json_invalid",
        finish_reason=expected,
        private=("forged-warning",),
    )


@pytest.mark.anyio
async def test_diagnostic_length_finish_does_not_reject_valid_multisentence_advice(caplog):
    summary = "肌肉量偏低，应重视肌肉保留。结合体重与身体成分调整训练。"
    training = "可选择弹力带划船、坐站练习。重视动作控制，按自身耐受循序渐进。"
    output = model_output(summary)
    output["interventions"] = [{"category": "training", "content": referenced_text(training)}]
    body = model_response(output).json()
    body["choices"][0]["finish_reason"] = "length"
    response = await run_diagnostic_transport(lambda request: httpx.Response(200, json=body))
    assert response.generation_mode == "model"
    assert response.overall_assessment == summary
    assert [item.category for item in response.interventions] == CATEGORIES
    assert (
        next(item.content for item in response.interventions if item.category == "training")
        == training
    )
    assert response.key_evidence == []
    assert not [
        record
        for record in caplog.records
        if record.name == _DIAGNOSTIC_LOGGER and record.levelno >= logging.WARNING
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_diagnostic_http_errors_do_not_log_sensitive_url_or_body(caplog, status):
    response = await run_diagnostic_transport(
        lambda request: httpx.Response(status, text=_PRIVATE_TEXT + _PRIVATE_TOKEN)
    )
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog, stage="request", reason="http_error", field="http_response", status_code=status
    )


@pytest.mark.anyio
@pytest.mark.parametrize("workflow", [False, True])
@pytest.mark.parametrize(
    ("error_type", "reason", "public_error"),
    [
        (httpx.ConnectError, "http_request_error", ReportAnalyzerError),
        (httpx.ReadTimeout, "timeout", TimeoutError),
    ],
)
async def test_diagnostic_request_errors_preserve_exception_and_fallback(
    caplog, workflow, error_type, reason, public_error
):
    def handler(request):
        raise error_type(f"{request.url} {_PRIVATE_TEXT} test-key", request=request)

    caplog.set_level("INFO", logger="weight_agent.report.workflow")
    if workflow:
        response = await run_diagnostic_transport(handler)
        assert_whole_report_fallback(response, make_request())
        completed = [
            record
            for record in caplog.records
            if record.name == "weight_agent.report.workflow"
            and record.getMessage().startswith("report_request_completed")
        ]
        assert len(completed) == 1
        assert completed[0].fallback_reason == (
            "timeout" if reason == "timeout" else "ReportAnalyzerError"
        )
    else:
        with pytest.raises(public_error):
            await run_diagnostic_transport(handler, workflow=False)
    assert_failure_diagnostic(
        caplog, stage="request", reason=reason, field="http_response", status_code=None
    )


@pytest.mark.anyio
@pytest.mark.parametrize("payload_failure", [False, True])
async def test_diagnostic_payload_and_unclassified_errors(caplog, monkeypatch, payload_failure):
    def fail(*args, **kwargs):
        raise ValueError(f"{_PRIVATE_TEXT} {_PRIVATE_TOKEN} test-key")

    calls = []

    def handler(request):
        calls.append(request)
        return model_response(model_output(_PRIVATE_TEXT))

    if payload_failure:
        monkeypatch.setattr(QwenReportAnalyzer, "_build_payload", fail)
    else:
        monkeypatch.setattr("weight_agent.report.qwen.resolve_metric_references", fail)
    response = await run_diagnostic_transport(handler)
    assert len(calls) == (0 if payload_failure else 1)
    assert_whole_report_fallback(response, make_request())
    assert_failure_diagnostic(
        caplog,
        stage="build_payload" if payload_failure else "resolve_references",
        reason="payload_error" if payload_failure else "analysis_error",
        field="request" if payload_failure else "$",
        status_code=None if payload_failure else 200,
    )


@pytest.mark.anyio
async def test_drop_mode_keeps_valid_blocks_and_fills_missing_categories(caplog):
    request = make_request()
    output = {
        "overall_assessment": referenced_text("肌肉量偏低。"),
        "interventions": [
            {"category": "direction", "content": referenced_text("以增肌和减脂为核心调整重点。")},
            {"category": "training", "content": referenced_text("每次练习三组。")},
            {"category": "nutrition", "content": referenced_text("优先保证蛋白质和规律饮食。")},
        ],
    }
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    assert response.overall_assessment == "肌肉量偏低。"
    by_category = {item.category: item.content for item in response.interventions}
    assert by_category["direction"] == "以增肌和减脂为核心调整重点。"
    assert by_category["nutrition"] == "优先保证蛋白质和规律饮食。"
    assert by_category["training"] == fallback_content(request, "training")
    assert by_category["weight"] == fallback_content(request, "weight")
    assert_drop_diagnostics(
        caplog,
        [
            {
                "reason": "literal_number",
                "field": "interventions[1].content.text",
                "validation_type": "none",
            }
        ],
    )


@pytest.mark.anyio
async def test_drop_mode_drops_all_invalid_interventions(caplog):
    request = make_request()
    output = {
        "overall_assessment": referenced_text("肌肉量偏低。"),
        "interventions": [
            {"category": "direction", "content": referenced_text("每次练习三组。")},
            {"category": "training", "content": referenced_text("每周训练三回。")},
        ],
    }
    response = await run_model_output(request, output)
    assert response.generation_mode == "model"
    assert response.overall_assessment == "肌肉量偏低。"
    by_category = {item.category: item.content for item in response.interventions}
    assert by_category == {category: fallback_content(request, category) for category in CATEGORIES}
    assert_drop_diagnostics(
        caplog,
        [
            {
                "reason": "literal_number",
                "field": "interventions[0].content.text",
                "validation_type": "none",
            },
            {
                "reason": "literal_number",
                "field": "interventions[1].content.text",
                "validation_type": "none",
            },
        ],
    )


@pytest.mark.anyio
async def test_drop_mode_overall_failure_still_falls_back(caplog):
    request = make_request()
    output = {
        "overall_assessment": referenced_text("体重120kg。"),
        "interventions": [
            {"category": "training", "content": referenced_text("循序渐进进行抗阻训练。")}
        ],
    }
    with pytest.raises(ValueError, match="unverified numeric reference"):
        resolve_metric_references(request, _ModelAnalysis.model_validate(output), drop_invalid=True)
    response = await run_model_output(request, output)
    assert_whole_report_fallback(response, request)
    assert_failure_diagnostic(
        caplog,
        stage="resolve_references",
        reason="literal_number",
        field="overall_assessment.text",
    )


def test_resolve_drop_invalid_records_controlled_details():
    request = make_request()
    analysis = _ModelAnalysis.model_validate(
        {
            "overall_assessment": referenced_text("肌肉量偏低。"),
            "interventions": [
                {"category": "direction", "content": referenced_text("每次练习三组。")},
                {"category": "training", "content": referenced_text("循序渐进进行抗阻训练。")},
            ],
        }
    )
    original = analysis.model_dump()
    dropped: list[dict[str, str]] = []
    result = resolve_metric_references(request, analysis, drop_invalid=True, dropped=dropped)
    assert dropped == [
        {
            "reason": "literal_number",
            "field": "interventions[0].content.text",
            "validation_type": "none",
        }
    ]
    assert result.overall_assessment == "肌肉量偏低。"
    assert [item.category for item in result.interventions] == ["training"]
    assert result.interventions[0].content == "循序渐进进行抗阻训练。"
    assert analysis.model_dump() == original
