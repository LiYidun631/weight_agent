"""Qwen 报告分析器：调用通义千问模型生成身体成分分析结果。"""

import json
import logging
import re
import time
from decimal import Decimal
from typing import Any

import httpx
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from weight_agent.api.schemas.report import ReportAnalysis, ReportRequest
from weight_agent.report.workflow import ReportAnalyzerError

PROMPT_VERSION = "report-v3"
logger = logging.getLogger(__name__)

# 生产版系统提示词：完整读取入参，但不生成后端负责的关键依据。
SYSTEM_PROMPT = """你是身体成分报告分析助手，面向普通用户生成中文整体分析和干预建议。

输入为 JSON，可能包含实际年龄、身高体重、身体类型、生物电阻抗、人体成分、
节段脂肪肌肉信息、评价建议、运动消耗量和节段等级。
字段均可能缺失，只分析实际提供且 value 不为空的数据；缺失不代表正常，不得猜测或补全。

输出：
1. overall_assessment：2-4 句，概括身体成分结构、主要问题和优先方向；可引用已提供的
   身体类型、身体得分或身体年龄。
2. interventions：按实际有参考数据的分类返回以下分类，每类最多 1 条；后端会为缺失分类补充兜底：
   direction（方向）、training（训练）、nutrition（营养）、weight（体重）、
   retest（复测建议）、body_status（身体状态）。

规则：
- 优先使用 level；没有 level 时才参考 value 与 standard_min/standard_max，边界值视为正常。
- unit、标准区间和等级都是输入事实，不换算、不改写、不补标准。
- 综合所有实际提供的数据，包括实际年龄、身体年龄、身体类型、生物电阻抗、身高体重 BMI、
  体脂、脂肪、肌肉、骨骼肌、水分、蛋白质、内脏脂肪、基础代谢、节段脂肪肌肉、
  评价等级、控制量和运动消耗量，不仅凭体重或 BMI 下结论。
- 相关异常合并说明；水分、蛋白质、无机盐、细胞水或身体细胞量随肌肉量偏低时，归入
  肌肉/瘦组织不足，不重复制定同类方案。
- 用节段数据识别局部重点，但不要逐项罗列。
- body_type、body_score、body_age 仅作体成分参考，不作疾病或医学诊断；没有名称时不要
  根据 code 猜名称。
- 控制量正值表示增加，负值表示减少，0 表示无需调整；不得自行计算控制量、变化量或百分比。
- 所有数值引用必须使用 {{字段路径}} 占位符，指向有 value 的指标对象，而不是 value 子字段。
  例如“体重 {{body_composition.weight_kg}}，BMI {{assessment.bmi}}”。
  后端会填入原始数值及单位，不要在占位符后重复写单位。不引用未提供 value 的指标。
  指标名称必须与引用路径一致，不得将体重、BMI、肌肉量等指标的路径互换。
  正文禁止直接写阿拉伯数字或中文数值；年龄比较只说明年轻、偏大或接近，不计算年龄差。
- 推荐摄入量为零时不作为营养目标，也不引用该零值，说明营养参考数据不足。
- 身体年龄较小不代表整体健康，仍须结合有实测值的体成分异常；实际年龄缺失时不比较年龄。
- 运动消耗仅用于选择运动方向，不代表用户已经完成该运动。
- 建议应通用、克制、可执行；不自行量化训练次数、时长或营养剂量，不给出诊断、治疗、药物
  或高风险处方。
- 有明确依据时优先输出方向、训练、营养、体重；有复测或体成分概况时再输出复测建议、
  身体状态。不要为了凑满 6 类而虚构内容。
- body_status 必须给出解释性结论：身体年龄存在时，与实际年龄比较并说明年轻、偏大或接近；
  结合身体得分、身体类型和明确异常的体脂/肌肉/水分等指标说明主要状态和改善重点，
  不能只用“身体得分 X 分；身体年龄 Y 岁”罗列数值。
- 某分类缺少对应参考数据时不要输出该分类，后端会单独补充该分类的兜底内容。

禁止：
- 输出 key_evidence、关键依据、逐项指标清单、标准区间计算过程或协议原始字节。
- 提及 user_id、measurement_id、measured_at 或模型名称。
- 输出 Markdown、解释文字或 Schema 外字段。
- 数据不足时，整体分析明确说明无法判断，interventions 返回空数组，不把缺失指标当成正常。
  数据充分且无明确异常时，才建议保持现有习惯。

严格按指定 JSON Schema 输出，仅包含 overall_assessment 和 interventions。
"""


class QwenReportAnalyzer:
    """基于通义千问 OpenAI 兼容接口的报告分析器。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        request_timeout_seconds: float,
        temperature: float = 0.2,
        max_completion_tokens: int = 1200,
        enable_thinking: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key  # API 密钥
        self._base_url = base_url.rstrip("/")  # 模型服务基础地址（去除尾部斜杠）
        self._model = model  # 模型名称
        self._request_timeout_seconds = request_timeout_seconds  # 请求超时（秒）
        self._temperature = temperature
        self._max_completion_tokens = max_completion_tokens
        self._enable_thinking = enable_thinking
        self._client = client  # 可注入的 HTTP 客户端（测试用）；为空时每次新建

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis:
        """调用模型分析身体成分数据并解析为结构化报告。"""
        del user_id  # 用户标识不应发送给模型服务方
        payload = self._build_payload(request)
        started = time.perf_counter()
        try:
            if self._client is not None:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self._request_timeout_seconds,
                )
            else:
                # 未注入客户端时，临时创建并自动关闭
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                        timeout=self._request_timeout_seconds,
                    )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Qwen returned empty content")
            result = ReportAnalysis.model_validate_json(content)
            result = resolve_metric_references(request, result)
            logger.info(
                "report_model_completed model=%s prompt_version=%s duration_ms=%d",
                self._model,
                PROMPT_VERSION,
                round((time.perf_counter() - started) * 1000),
            )
            return result
        except httpx.TimeoutException as exc:
            raise TimeoutError("Report model request timed out") from exc
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            logger.warning(
                "report_model_failed model=%s prompt_version=%s duration_ms=%d error=%s",
                self._model,
                PROMPT_VERSION,
                round((time.perf_counter() - started) * 1000),
                type(exc).__name__,
            )
            # 统一包装为报告分析异常，由上层工作流降级处理
            raise ReportAnalyzerError("Qwen report analysis failed") from exc

    def _headers(self) -> dict[str, str]:
        """构造请求头：Bearer 认证 + JSON 内容类型。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, request: ReportRequest) -> dict[str, Any]:
        """构造模型请求体：系统提示词 + 身体成分数据 + 严格 JSON Schema 约束。"""
        # 排除测量 ID、测量时间与关键依据，仅发送分析所需数据
        report_data = request.model_dump(
            exclude={"measurement_id", "measured_at"},
            exclude_none=True,
        )
        schema = ReportAnalysis.model_json_schema()
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "请分析以下身体成分数据：\n"
                    + json.dumps(
                        jsonable_encoder(report_data),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            # 要求模型严格按 JSON Schema 输出，保证可解析性
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "body_composition_report",
                    "strict": True,
                    "schema": schema,
                },
            },
            "temperature": self._temperature,
            "max_completion_tokens": self._max_completion_tokens,
            "enable_thinking": self._enable_thinking,
        }


# 字段名中的数字属于路径，不是模型生成的数值。
_METRIC_REFERENCE = re.compile(r"\{\{([a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)+)\}\}")
_METRIC_LABELS = {
    "subject.height_cm": ("身高",),
    "subject.age_years": ("实际年龄",),
    "body_composition.weight_kg": ("体重", "body weight", "weight"),
    "body_composition.total_body_water_kg": ("水分", "总水分", "身体总水分"),
    "body_composition.body_fat_mass_kg": ("体脂量", "脂肪量"),
    "body_composition.protein_mass_kg": ("蛋白质", "蛋白质量"),
    "body_composition.mineral_mass_kg": ("无机盐", "无机盐量"),
    "body_composition.fat_free_mass_kg": ("去脂体重", "去脂体质量"),
    "body_composition.muscle_mass_kg": ("肌肉量",),
    "body_composition.bone_mass_kg": ("骨量",),
    "body_composition.skeletal_muscle_mass_kg": ("骨骼肌量",),
    "body_composition.intracellular_water_kg": ("细胞内水分",),
    "body_composition.extracellular_water_kg": ("细胞外水分",),
    "body_composition.body_cell_mass_kg": ("身体细胞量",),
    "body_composition.subcutaneous_fat_mass_kg": ("皮下脂肪量",),
    "assessment.body_score": ("身体得分", "身体评分"),
    "assessment.body_age": ("身体年龄",),
    "assessment.skeletal_muscle_index": ("骨骼肌指数", "smi"),
    "assessment.waist_hip_ratio": ("腰臀比",),
    "assessment.visceral_fat_level": ("内脏脂肪等级", "内脏脂肪"),
    "assessment.obesity_degree_percent": ("肥胖度",),
    "assessment.bmi": ("bmi", "体质指数", "身体质量指数"),
    "assessment.body_fat_rate_percent": ("体脂率", "脂肪率"),
    "assessment.basal_metabolism_kcal": ("基础代谢", "基础代谢率"),
    "assessment.recommended_calorie_intake_kcal": ("推荐摄入量", "推荐热量摄入"),
    "assessment.ideal_body_weight_kg": ("理想体重",),
    "assessment.target_weight_kg": ("目标体重",),
    "assessment.weight_control_kg": ("体重控制量",),
    "assessment.muscle_control_kg": ("肌肉控制量",),
    "assessment.fat_control_kg": ("脂肪控制量",),
    "assessment.subcutaneous_fat_rate_percent": ("皮下脂肪率",),
    "segmental_composition.fat_mass_kg.": ("脂肪量", "体脂量"),
    "segmental_composition.fat_rate_percent.": ("脂肪率", "体脂率"),
    "segmental_composition.muscle_mass_kg.": ("肌肉量",),
    "segmental_composition.muscle_rate_percent.": ("肌肉率",),
}
_LABEL_PATTERN = "|".join(
    re.escape(label)
    for label in sorted(
        {label for labels in _METRIC_LABELS.values() for label in labels}, key=len, reverse=True
    )
)
_LABEL_SEPARATOR = (
    r"\s*(?:(?:的)?(?:数值|参考值|测量值|参考|约为|为|是|约|达到|等于)\s*)*[:：=（(]?\s*"
)
_ADJACENT_METRIC_LABEL = re.compile(rf"({_LABEL_PATTERN}){_LABEL_SEPARATOR}$", re.IGNORECASE)
_CHINESE_DIGITS = "零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億"
_NUMBER_UNITS = r"千克|公斤|kg|厘米|cm|岁|年|级|大卡|kcal|次|分钟|小时|周|天|%|％"
_LITERAL_NUMBER = re.compile(
    rf"\d|百分之|[{_CHINESE_DIGITS}]{{2,}}|"
    rf"[{_CHINESE_DIGITS}]+(?:点[{_CHINESE_DIGITS}]+)?\s*(?:{_NUMBER_UNITS})|"
    rf"(?:{_LABEL_PATTERN}){_LABEL_SEPARATOR}[{_CHINESE_DIGITS}]+分?"
    r"(?=$|[，。；、！？\s]|偏高|偏低|正常|过高|过低)|"
    rf"[{_CHINESE_DIGITS}]+分(?=$|[，。；、！？\s])",
    re.IGNORECASE,
)
_NON_NUMERIC_PHRASES = re.compile(
    rf"(?<![{_CHINESE_DIGITS}])(?:万一|一一)(?![{_CHINESE_DIGITS}])|千万(?=不要|不能|别)"
)


def resolve_metric_references(request: ReportRequest, analysis: ReportAnalysis) -> ReportAnalysis:
    data = request.model_dump(exclude={"measurement_id", "measured_at"})

    def render(text: str) -> str:
        # 保留占位符的分隔作用，避免删除后把两侧文字拼成虚假的数字。
        plain_text = _METRIC_REFERENCE.sub("，", text)
        plain_text = _NON_NUMERIC_PHRASES.sub("", plain_text)
        if "{" in plain_text or "}" in plain_text or _LITERAL_NUMBER.search(plain_text):
            raise ValueError("Model output contains an unverified numeric reference")

        def replace(match: re.Match[str]) -> str:
            path = match.group(1)
            metric: Any = data
            for part in path.split("."):
                if not isinstance(metric, dict) or part not in metric:
                    raise ValueError("Model referenced an unknown metric")
                metric = metric[part]
            if not isinstance(metric, dict) or not isinstance(metric.get("value"), Decimal):
                raise ValueError("Model referenced a missing or non-numeric metric")
            if path == "assessment.recommended_calorie_intake_kcal" and metric["value"] == 0:
                raise ValueError("Zero calorie intake is not a usable nutrition reference")
            # 只核对紧邻引用的明确指标名，不把跨句定性描述误当作数值标签。
            label = _ADJACENT_METRIC_LABEL.search(text[: match.start()])
            if label is not None and not any(
                label.group(1).lower() in labels
                and (path.startswith(key) if key.endswith(".") else path == key)
                for key, labels in _METRIC_LABELS.items()
            ):
                raise ValueError("Model metric label does not match its reference")
            value = format(metric["value"], "f")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
            return value + (metric.get("unit") or "")

        return _METRIC_REFERENCE.sub(replace, text)

    rendered = analysis.model_dump()
    rendered["overall_assessment"] = render(analysis.overall_assessment)
    for item in rendered["interventions"]:
        item["content"] = render(item["content"])
    return ReportAnalysis.model_validate(rendered)
