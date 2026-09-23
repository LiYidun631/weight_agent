"""报告工作流：编排模型分析，失败或超时时自动降级为规则兜底。"""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from weight_agent.api.schemas.report import (
    Intervention,
    ReferenceMetric,
    ReportAnalysis,
    ReportRequest,
    ReportResponse,
)

FALLBACK_SUMMARY = "本次报告已根据身体成分数据生成基础建议。"
DEFAULT_RECOMMENDATION = "保持现有饮食与运动习惯即可。"
REQUIRED_INTERVENTION_CATEGORIES = (
    "direction",
    "training",
    "nutrition",
    "weight",
    "retest",
    "body_status",
)
logger = logging.getLogger(__name__)
console_logger = logging.getLogger("uvicorn.error")


class ReportAnalyzerError(RuntimeError):
    """模型分析无法完成时抛出。"""


class ReportAnalyzerUnavailableError(ReportAnalyzerError):
    """模型分析器未配置时抛出。"""


class ReportAnalyzer(Protocol):
    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis: ...


class KeyEvidenceProvider(Protocol):
    """后端关键依据生成器协议。"""

    def build(self, request: ReportRequest) -> list[dict[str, Any]]: ...


class UnavailableReportAnalyzer:
    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis:
        raise ReportAnalyzerUnavailableError("Report model analyzer is not configured")


class EmptyKeyEvidenceProvider:
    """关键依据模块接入前的空实现。"""

    def build(self, request: ReportRequest) -> list[dict[str, Any]]:
        del request
        return []


class ReportWorkflow:
    """报告生成工作流：模型失败时使用规则兜底。"""

    def __init__(
        self,
        analyzer: ReportAnalyzer,
        timeout_seconds: float,
        key_evidence_provider: KeyEvidenceProvider | None = None,
    ) -> None:
        self._analyzer = analyzer
        self._timeout_seconds = timeout_seconds
        self._key_evidence_provider = key_evidence_provider or EmptyKeyEvidenceProvider()

    async def run(self, request: ReportRequest, user_id: str) -> ReportResponse:
        request_id = f"req_{uuid4().hex}"
        started_at = time.perf_counter()
        model_started_at = started_at
        model_duration_ms = 0.0
        fallback_reason: str | None = None
        model_called = not isinstance(self._analyzer, UnavailableReportAnalyzer)
        try:
            if model_called:
                console_logger.info(
                    "[REPORT_MODEL] calling model=%s",
                    getattr(self._analyzer, "model_name", "unavailable"),
                )
            model_started_at = time.perf_counter()
            analysis = await asyncio.wait_for(
                self._analyzer.analyze(request, user_id),
                timeout=self._timeout_seconds,
            )
            model_duration_ms = round((time.perf_counter() - model_started_at) * 1000, 2)
        except TimeoutError:
            model_duration_ms = round((time.perf_counter() - model_started_at) * 1000, 2)
            analysis = build_fallback_analysis(request)
            generation_mode = "fallback"
            fallback_reason = "timeout"
        except ReportAnalyzerError as exc:
            model_duration_ms = round((time.perf_counter() - model_started_at) * 1000, 2)
            analysis = build_fallback_analysis(request)
            generation_mode = "fallback"
            fallback_reason = type(exc).__name__
        else:
            generation_mode = "model"

        model_categories = (
            {
                item.category
                for item in analysis.interventions
                if item.category in REQUIRED_INTERVENTION_CATEGORIES
            }
            if generation_mode == "model"
            else set()
        )
        fallback_categories = [
            category
            for category in REQUIRED_INTERVENTION_CATEGORIES
            if category not in model_categories
        ]
        if generation_mode == "model" and fallback_categories:
            analysis = merge_model_analysis(analysis, build_fallback_analysis(request))
        else:
            analysis = merge_model_analysis(analysis, analysis)
        total_duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        source_text = "模型生成" if generation_mode == "model" else "规则兜底"
        console_logger.info(
            "[REPORT_RESULT] source=%s model_called=%s model=%s "
            "model_categories=%s fallback_categories=%s fallback_reason=%s "
            "model_duration_ms=%.2f duration_ms=%.2f",
            source_text,
            model_called,
            getattr(self._analyzer, "model_name", "unavailable"),
            ",".join(
                category
                for category in REQUIRED_INTERVENTION_CATEGORIES
                if category in model_categories
            )
            or "none",
            ",".join(fallback_categories) or "none",
            fallback_reason or "none",
            model_duration_ms,
            total_duration_ms,
        )
        logger.info(
            "report_request_completed request_id=%s generation_mode=%s model=%s "
            "prompt_version=%s model_duration_ms=%.2f total_duration_ms=%.2f "
            "fallback_reason=%s",
            request_id,
            generation_mode,
            getattr(self._analyzer, "model_name", None),
            getattr(self._analyzer, "prompt_version", None),
            model_duration_ms,
            total_duration_ms,
            fallback_reason or "none",
            extra={
                "request_id": request_id,
                "generation_mode": generation_mode,
                "model": getattr(self._analyzer, "model_name", None),
                "prompt_version": getattr(self._analyzer, "prompt_version", None),
                "model_duration_ms": model_duration_ms,
                "total_duration_ms": total_duration_ms,
                "fallback_reason": fallback_reason,
            },
        )

        return ReportResponse(
            request_id=request_id,
            generation_mode=generation_mode,
            overall_assessment=analysis.overall_assessment,
            key_evidence=self._key_evidence_provider.build(request),
            interventions=analysis.interventions,
        )


def build_fallback_analysis(request: ReportRequest) -> ReportAnalysis:
    """根据评估指标为六个分类生成规则化的基础建议。"""
    assessment = request.assessment
    interventions: list[Intervention] = []

    muscle_control = _metric_value(assessment.muscle_control_kg)
    fat_control = _metric_value(assessment.fat_control_kg)
    ideal_weight = _metric_value(assessment.ideal_body_weight_kg)
    weight_control = _metric_value(assessment.weight_control_kg)
    recommended_intake = _metric_value(assessment.recommended_calorie_intake_kcal)

    direction_recommendations: list[str] = []
    if muscle_control is not None and muscle_control > 0:
        amount = _format_decimal(muscle_control, include_positive_sign=True)
        direction_recommendations.append(f"增肌（肌肉控制量 {amount}kg）")
    if fat_control is not None and fat_control < 0:
        amount = _format_decimal(fat_control)
        direction_recommendations.append(f"减脂（脂肪控制量 {amount}kg）")
    interventions.append(
        Intervention(
            category="direction",
            content=(
                f"以{'、'.join(direction_recommendations)}为核心，按身体成分变化调整重点。"
                if direction_recommendations
                else "暂无明确体成分调整方向，建议结合完整评估数据判断。"
            ),
        )
    )

    training_recommendations: list[str] = []
    if muscle_control is not None and muscle_control > 0:
        training_recommendations.append("循序渐进进行抗阻训练，以支持肌肉增长")
    if fat_control is not None and fat_control < 0:
        training_recommendations.append("结合自身耐受情况安排有氧活动，以支持减脂")
    if training_recommendations:
        training_content = "；".join(training_recommendations) + "。"
    elif muscle_control == 0 and fat_control == 0:
        training_content = "肌肉与脂肪控制量均为 0，无需据此新增增肌或减脂训练目标。"
    else:
        training_content = "暂无明确增肌或减脂训练依据，建议结合完整评估和运动能力判断。"
    interventions.append(Intervention(category="training", content=training_content))

    if weight_control == 0:
        weight_content = "体重控制量为 0，无需据此调整体重，继续观察身体成分变化。"
    elif ideal_weight is not None and weight_control is not None:
        ideal_weight_text = _format_decimal(ideal_weight)
        control_amount = _format_decimal(weight_control, include_positive_sign=True)
        direction = "增加" if weight_control > 0 else "减少"
        weight_content = (
            f"理想体重 {ideal_weight_text}kg，体重控制量 {control_amount}kg，"
            f"表示建议{direction}体重；应结合肌肉与脂肪变化，不只看体重升降。"
        )
    else:
        weight_content = "暂无足够体重控制参考数据，建议补充理想体重和体重控制量后评估。"
    interventions.append(Intervention(category="weight", content=weight_content))

    # 0 可作为输入，但不能作为每日摄入目标。
    if recommended_intake is not None and recommended_intake > 0:
        nutrition_content = (
            f"每日摄入参考 {_format_decimal(recommended_intake)}kCal，优先保证蛋白质和规律饮食。"
        )
    else:
        nutrition_content = "暂无足够营养参考数据，建议补充基础代谢或推荐摄入量后评估。"
    interventions.append(Intervention(category="nutrition", content=nutrition_content))

    interventions.append(
        Intervention(
            category="retest",
            content="建议 4 周后复测，重点观察体脂率、肌肉量及本次干预目标的变化。",
        )
    )

    body_status_content = _build_body_status_content(request)
    interventions.append(Intervention(category="body_status", content=body_status_content))

    return ReportAnalysis(
        overall_assessment=FALLBACK_SUMMARY,
        interventions=interventions,
    )


def _build_body_status_content(request: ReportRequest) -> str:
    """生成有比较和结论的身体状态说明，避免只罗列身体得分和年龄。"""
    assessment = request.assessment
    body_score = _metric_value(assessment.body_score)
    body_age = _metric_value(assessment.body_age)
    actual_age = _metric_value(request.subject.age_years)
    body_type = assessment.body_type.name

    statements: list[str] = []
    if body_score is not None:
        statements.append(f"身体得分 {_format_decimal(body_score)} 分")

    if body_age is not None:
        age_statement = f"身体年龄 {_format_decimal(body_age)} 岁"
        if actual_age is not None:
            age_gap = actual_age - body_age
            if age_gap > 0:
                age_statement += (
                    f"，较实际年龄 {_format_decimal(actual_age)} 岁年轻 "
                    f"{_format_decimal(age_gap)} 岁"
                )
            elif age_gap < 0:
                age_statement += (
                    f"，较实际年龄 {_format_decimal(actual_age)} 岁偏大 "
                    f"{_format_decimal(abs(age_gap))} 岁"
                )
            else:
                age_statement += f"，与实际年龄 {_format_decimal(actual_age)} 岁基本一致"
        statements.append(age_statement)

    if body_type:
        statements.append(f"身体类型为{body_type}")

    abnormal_items = _collect_abnormal_component_names(request)
    if abnormal_items:
        statements.append(f"当前改善重点主要集中在{'、'.join(abnormal_items[:3])}")
    elif statements:
        # 年龄比较不代表整体健康状况。
        statements.append("建议结合体脂、肌肉和水分等指标持续观察变化")

    if not statements:
        return "暂无足够身体状态参考数据，建议补充身体得分、身体年龄或身体类型。"
    return "；".join(statements) + "。"


def _collect_abnormal_component_names(request: ReportRequest) -> list[str]:
    """提取有实测值且有等级或参考区间依据的异常项目。"""
    labels = {
        "total_body_water_kg": "水分",
        "body_fat_mass_kg": "体脂量",
        "protein_mass_kg": "蛋白质",
        "muscle_mass_kg": "肌肉量",
        "bone_mass_kg": "骨量",
        "skeletal_muscle_mass_kg": "骨骼肌量",
        "intracellular_water_kg": "细胞内水分",
        "extracellular_water_kg": "细胞外水分",
        "body_cell_mass_kg": "身体细胞量",
        "weight_kg": "体重",
        "mineral_mass_kg": "无机盐量",
        "fat_free_mass_kg": "去脂体重",
        "subcutaneous_fat_mass_kg": "皮下脂肪量",
    }
    abnormal: list[str] = []
    for field_name, label in labels.items():
        metric = getattr(request.body_composition, field_name)
        if _is_abnormal_metric(metric):
            abnormal.append(label)

    assessment_labels = {
        "bmi": "BMI",
        "body_fat_rate_percent": "体脂率",
        "visceral_fat_level": "内脏脂肪等级",
        "waist_hip_ratio": "腰臀比",
        "skeletal_muscle_index": "骨骼肌指数",
        "obesity_degree_percent": "肥胖度",
        "subcutaneous_fat_rate_percent": "皮下脂肪率",
        "basal_metabolism_kcal": "基础代谢",
    }
    # 推荐摄入、控制量和运动消耗不是异常健康指标。
    for field_name, label in assessment_labels.items():
        if _is_abnormal_metric(getattr(request.assessment, field_name)):
            abnormal.append(label)

    segment_labels = {
        "right_arm": "右上臂",
        "left_arm": "左上臂",
        "trunk": "躯干",
        "right_leg": "右腿",
        "left_leg": "左腿",
    }
    segment_metrics = (
        ("fat_mass_kg", "脂肪量", None),
        ("fat_rate_percent", "体脂率", "fat"),
        ("muscle_mass_kg", "肌肉量", None),
        ("muscle_rate_percent", "肌肉率", "muscle"),
    )
    for field_name, label, standard_group in segment_metrics:
        metrics = getattr(request.segmental_composition, field_name)
        # 节段评级对应比例指标，不将比例等级套用到质量指标。
        standards = getattr(request.segment_standards, standard_group) if standard_group else None
        for segment, segment_label in segment_labels.items():
            if _is_abnormal_metric(
                getattr(metrics, segment),
                segment_standard=getattr(standards, segment) if standards is not None else None,
            ):
                abnormal.append(f"{segment_label}{label}")
    return abnormal


def _is_abnormal_metric(metric: ReferenceMetric, *, segment_standard: int | None = None) -> bool:
    """仅依据已提供的异常证据判断，不推测设备等级或医学阈值。"""
    # 无实测值时，等级和标准不能独立构成异常证据。
    if metric.value is None:
        return False
    level = str(metric.level).strip().lower() if metric.level is not None else ""
    # 非空等级优先；正常、未知及数字等级均不由区间或节段评级覆盖。
    if level:
        return level in {"low", "低", "偏低", "high", "高", "偏高", "abnormal", "异常"}
    if metric.standard_min is not None or metric.standard_max is not None:
        return (metric.standard_min is not None and metric.value < metric.standard_min) or (
            metric.standard_max is not None and metric.value > metric.standard_max
        )
    # 仅无自身等级和区间时使用节段协议：0 偏低、1 正常、2 偏高。
    return segment_standard in (0, 2)


def merge_model_analysis(
    model_analysis: ReportAnalysis,
    fallback_analysis: ReportAnalysis,
) -> ReportAnalysis:
    """只为模型缺失的分类补兜底，保留其他分类的模型结果。"""
    model_by_category = {
        item.category: item
        for item in model_analysis.interventions
        if item.category in REQUIRED_INTERVENTION_CATEGORIES
    }
    fallback_by_category = {item.category: item for item in fallback_analysis.interventions}
    interventions = [
        model_by_category.get(category, fallback_by_category[category])
        for category in REQUIRED_INTERVENTION_CATEGORIES
    ]
    return ReportAnalysis(
        overall_assessment=model_analysis.overall_assessment,
        interventions=interventions,
    )


def _metric_value(metric: ReferenceMetric) -> Decimal | None:
    return metric.value


def _format_decimal(value: Decimal, *, include_positive_sign: bool = False) -> str:
    """格式化小数值：去掉多余尾零，可选择在正数前加 + 号。"""
    normalized = value.normalize()
    text = format(normalized, "f")
    if include_positive_sign and value > 0:
        return f"+{text}"
    return text
