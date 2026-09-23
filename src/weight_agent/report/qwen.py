"""Qwen 报告分析器：调用通义千问模型生成身体成分分析结果。"""

import json
import logging
import re
import time
from decimal import Decimal
from typing import Any, Literal

import httpx
from pydantic import Field, ValidationError

from weight_agent.api.schemas.report import ReportAnalysis, ReportRequest, ReportSchema
from weight_agent.report.workflow import ReportAnalyzerError

PROMPT_VERSION = "report-v8"
logger = logging.getLogger(__name__)
console_logger = logging.getLogger("uvicorn.error")

SYSTEM_PROMPT = """你是身体成分报告分析助手。请根据输入中实际存在的指标，
生成普通用户能理解的中文分析。

只输出 JSON，不要 Markdown、解释或额外字段：
{
  "overall_assessment": "整体分析",
  "interventions": [
    {"category":"direction","content":"方向建议"},
    {"category":"training","content":"训练建议"},
    {"category":"nutrition","content":"营养建议"},
    {"category":"weight","content":"体重建议"},
    {"category":"retest","content":"复测建议"},
    {"category":"body_status","content":"身体状态"}
  ]
}

六类 category 最多各一条；有依据就生成，没有依据可省略，后端会补充。只分析非空数据，
缺失不代表正常，不猜测、不补全、不做医学诊断。综合年龄、身体类型、身高体重、BMI、
阻抗、人体成分、节段脂肪肌肉、评价指标和运动消耗。

文案要求：整体分析简洁但说明主要问题和优先方向；建议具体、克制、可执行。body_status
必须解释身体得分、身体年龄、身体类型或异常指标之间的关系，不能只罗列数值。不要输出
key_evidence、用户标识、协议原始字节或计算过程。文案中可以自然引用输入中的数值和单位。

兼容说明：后端可能提供 reference_id 和 references；如收到旧版结构，按对应结构返回。
有体重、体成分、节段、阻抗等身体实测值时，必须输出 retest，不需要历史记录或专门复测字段。
提醒使用同一设备、相近时段及相近身体状态复测，不编造复测周期，references 可为 []。
仅年龄、性别或运动消耗不满足此条件；有部分实测但不足以综合判断时说明局限，仍输出 retest。
整体分析用 2-4 句；各建议按需要展开依据、具体行动和注意事项，不为缩短篇幅省略针对性内容。
training 说明运动类型、动作示例和执行要点；body_status 说明状态与改善重点，不只罗列数值。
"""

LITE_SYSTEM_PROMPT = """你是身体成分报告分析助手，面向普通用户生成简洁、易懂的中文分析。

只输出 JSON，不要 Markdown、解释或额外字段：
{
  "overall_assessment": "整体分析",
  "interventions": [
    {"category":"direction","content":"方向建议"},
    {"category":"training","content":"训练建议"},
    {"category":"nutrition","content":"营养建议"},
    {"category":"weight","content":"体重建议"},
    {"category":"retest","content":"复测建议"},
    {"category":"body_status","content":"身体状态"}
  ]
}

只使用输入中实际存在且非空的指标；缺失不代表正常，不猜测、不补全、不做医学诊断。
综合年龄、性别、身高体重、BMI、身体类型、全身和节段成分、阻抗、评价及运动消耗。
有依据就输出对应分类，每类最多一条；没有依据可以省略，后端会补齐。没有身体实测值时，
整体说明数据不足，interventions 返回 []；有身体实测值时输出 retest，并提醒：
同一设备、相近时段和相近身体状态复测。
不编造复测周期。输入有肌肉、脂肪、节段、阻抗或控制量时，
建议要说明与指标的关系和改善重点。

overall_assessment 用 2-4 句；training 给出合适的运动类型、动作示例和执行要点；
body_status 必须解释身体得分、身体年龄、身体类型或明确异常之间的关系，不能只罗列数值。
建议具体、克制、可执行，不量化训练次数、时长或营养剂量，不给诊断、治疗或药物建议。
可以自然引用输入中的数值和单位，但不要输出 key_evidence、用户标识、协议原始字节、
计算过程、Markdown 或 Schema 外字段。"""


class _ReferencedText(ReportSchema):
    text: str = Field(min_length=1, max_length=800)
    references: list[str]


class _InterventionText(_ReferencedText):
    text: str = Field(min_length=1, max_length=500)


class _ModelIntervention(ReportSchema):
    category: Literal["direction", "training", "nutrition", "weight", "retest", "body_status"]
    content: _InterventionText


class _ModelAnalysis(ReportSchema):
    overall_assessment: _ReferencedText
    interventions: list[_ModelIntervention] = Field(max_length=6)


class _LiteIntervention(ReportSchema):
    category: Literal["direction", "training", "nutrition", "weight", "retest", "body_status"]
    content: str = Field(min_length=1, max_length=500)


class _LiteModelAnalysis(ReportSchema):
    overall_assessment: str = Field(min_length=1, max_length=800)
    interventions: list[_LiteIntervention] = Field(max_length=6)


class _ModelOutputError(ValueError):
    def __init__(self, message: str, *, reason: str, field: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.field = field


def _parse_model_output(raw_output: object) -> ReportAnalysis:
    """解析轻量 v8 输出；兼容旧版带 text/references 的响应。"""
    if not isinstance(raw_output, dict):
        raise _ModelOutputError(
            "Model output must be an object",
            reason="invalid_model_structure",
            field="$",
        )
    overall = raw_output.get("overall_assessment")
    interventions = raw_output.get("interventions")
    if isinstance(overall, dict):
        overall_text = overall.get("text")
        if not isinstance(overall_text, str):
            raise _ModelOutputError(
                "Invalid overall assessment",
                reason="invalid_model_structure",
                field="overall_assessment",
            )
    else:
        overall_text = overall
    if not isinstance(overall_text, str) or not overall_text.strip():
        raise _ModelOutputError(
            "Invalid overall assessment",
            reason="invalid_model_structure",
            field="overall_assessment",
        )
    if not isinstance(interventions, list):
        raise _ModelOutputError(
            "Invalid interventions",
            reason="invalid_model_structure",
            field="interventions",
        )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(interventions):
        if not isinstance(item, dict):
            raise _ModelOutputError(
                "Invalid intervention",
                reason="invalid_model_structure",
                field=f"interventions[{index}]",
            )
        category = item.get("category")
        content = item.get("content")
        if isinstance(content, dict):
            content = content.get("text")
        if not isinstance(category, str) or not isinstance(content, str) or not content.strip():
            raise _ModelOutputError(
                "Invalid intervention",
                reason="invalid_model_structure",
                field=f"interventions[{index}]",
            )
        if category in seen:
            raise _ModelOutputError(
                "Duplicate intervention category",
                reason="duplicate_category",
                field="interventions",
            )
        seen.add(category)
        normalized.append({"category": category, "content": content})
    parsed = _LiteModelAnalysis.model_validate(
        {"overall_assessment": overall_text, "interventions": normalized}
    )
    return ReportAnalysis(
        overall_assessment=parsed.overall_assessment,
        interventions=[
            {"category": item.category, "content": item.content}
            for item in parsed.interventions
        ],
    )


def _failure_details(exc: Exception, stage: str) -> tuple[str, str, str]:
    if isinstance(exc, _ModelOutputError):
        validation_type = "json_invalid" if exc.reason == "invalid_model_json" else "none"
        return exc.reason, exc.field, validation_type
    if isinstance(exc, ValidationError):
        # Pydantic 的 input、msg、ctx 和未知字段名可能包含原文，只保留受控类型和结构位置。
        error = exc.errors(include_url=False, include_context=False, include_input=False)[0]
        location = error["loc"]
        fields = {
            "overall_assessment",
            "interventions",
            "category",
            "content",
            "text",
            "references",
        }
        field = ""
        for part in location:
            if isinstance(part, int):
                field += f"[{part}]"
            else:
                field += ("." if field else "") + (part if part in fields else "<extra>")
        error_type = error["type"]
        if error_type == "json_invalid":
            reason = "invalid_model_json"
        elif error_type == "string_too_long":
            reason = "rendered_text_too_long" if stage == "resolve_references" else "text_too_long"
        elif (
            stage == "resolve_references"
            and location == ("interventions",)
            and error_type == "value_error"
        ):
            reason = "duplicate_category"
        else:
            reason = "schema_validation"
        return reason, field or "$", error_type
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "http_response", "none"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_error", "http_response", "none"
    if isinstance(exc, httpx.HTTPError):
        return "http_request_error", "http_response", "none"
    if stage == "build_payload":
        return "payload_error", "request", "none"
    if stage == "response_json":
        return "invalid_response_json", "response", "none"
    if stage == "response_content":
        return "invalid_response_structure", "choices[0].message.content", "none"
    return "analysis_error", "$", "none"


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
        strict_output_validation: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """初始化报告分析器配置。

        参数均为关键字参数；可注入自定义 httpx 客户端以便测试复用连接。
        """
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._request_timeout_seconds = request_timeout_seconds
        self._temperature = temperature
        self._max_completion_tokens = max_completion_tokens
        self._enable_thinking = enable_thinking
        self._strict_output_validation = strict_output_validation
        self._client = client

    @property
    def model_name(self) -> str:
        """返回当前配置的模型名称。"""
        return self._model

    @property
    def prompt_version(self) -> str:
        """返回系统提示词的版本号。"""
        return PROMPT_VERSION

    async def analyze(self, request: ReportRequest, user_id: str) -> ReportAnalysis:
        """调用模型分析身体成分数据并解析为结构化报告。"""
        del user_id  # 用户标识不应发送给模型服务方
        started = time.perf_counter()
        stage = "build_payload"
        finish_reason = "unknown"
        status_code: int | None = None
        try:
            payload = self._build_payload(request)
            stage = "request"
            if self._client is not None:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self._request_timeout_seconds,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        headers=self._headers(),
                        json=payload,
                        timeout=self._request_timeout_seconds,
                    )
            status_code = response.status_code
            response.raise_for_status()
            stage = "response_json"
            body = response.json()
            stage = "response_content"
            content = body["choices"][0]["message"]["content"]
            raw_finish_reason = body["choices"][0].get("finish_reason")
            if isinstance(raw_finish_reason, str) and raw_finish_reason in {
                "stop",
                "length",
                "content_filter",
                "tool_calls",
                "function_call",
            }:
                finish_reason = raw_finish_reason
            if content is None or (isinstance(content, str) and not content.strip()):
                raise _ModelOutputError(
                    "Qwen returned empty content",
                    reason="empty_content",
                    field="choices[0].message.content",
                )
            if not isinstance(content, str):
                raise _ModelOutputError(
                    "Qwen returned non-text content",
                    reason="invalid_content_type",
                    field="choices[0].message.content",
                )
            stage = "model_schema"
            try:
                raw_output = json.loads(content)
            except json.JSONDecodeError as exc:
                raise _ModelOutputError(
                    "Model returned invalid JSON",
                    reason="invalid_model_json",
                    field="$",
                ) from exc
            if self._strict_output_validation:
                analysis = _ModelAnalysis.model_validate(raw_output)
                stage = "resolve_references"
                dropped: list[dict[str, str]] = []
                result = resolve_metric_references(
                    request, analysis, drop_invalid=True, dropped=dropped
                )
                for entry in dropped:
                    logger.warning(
                        "report_intervention_dropped model=%s prompt_version=%s stage=%s "
                        "reason=%s field=%s validation_type=%s",
                        self._model,
                        PROMPT_VERSION,
                        "resolve_references",
                        entry["reason"],
                        entry["field"],
                        entry["validation_type"],
                        extra={
                            "failure_stage": "resolve_references",
                            "failure_reason": entry["reason"],
                            "failure_field": entry["field"],
                            "validation_type": entry["validation_type"],
                        },
                    )
            else:
                result = _parse_model_output(raw_output)
            logger.info(
                "report_model_completed model=%s prompt_version=%s duration_ms=%d dropped=%d",
                self._model,
                PROMPT_VERSION,
                round((time.perf_counter() - started) * 1000),
                len(dropped) if self._strict_output_validation else 0,
            )
            return result
        except (
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            reason, field, validation_type = _failure_details(exc, stage)
            duration_ms = round((time.perf_counter() - started) * 1000)
            logger.warning(
                "report_model_failed model=%s prompt_version=%s duration_ms=%d error=%s "
                "stage=%s reason=%s field=%s validation_type=%s finish_reason=%s status_code=%s",
                self._model,
                PROMPT_VERSION,
                duration_ms,
                type(exc).__name__,
                stage,
                reason,
                field,
                validation_type,
                finish_reason,
                status_code if status_code is not None else "none",
                extra={
                    "failure_stage": stage,
                    "failure_reason": reason,
                    "failure_field": field,
                    "validation_type": validation_type,
                    "finish_reason": finish_reason,
                    "status_code": status_code,
                },
            )
            console_logger.warning(
                "[REPORT_MODEL_FAILED] model=%s stage=%s reason=%s field=%s "
                "validation_type=%s duration_ms=%d",
                self._model,
                stage,
                reason,
                field,
                validation_type,
                duration_ms,
            )
            if isinstance(exc, httpx.TimeoutException):
                raise TimeoutError("Report model request timed out") from exc
            raise ReportAnalyzerError("Qwen report analysis failed") from exc

    def _headers(self) -> dict[str, str]:
        """构造请求头：Bearer 鉴权与 JSON 内容类型。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, request: ReportRequest) -> dict[str, Any]:
        """组装发往模型的请求体。

        将系统提示词与带编号的指标数据组合为消息，
        并通过 JSON Schema 约束模型的输出格式。
        """
        report_data, _ = _reference_data(request)
        return {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        SYSTEM_PROMPT
                        if self._strict_output_validation
                        else LITE_SYSTEM_PROMPT
                    ),
                },
                {
                    "role": "user",
                    "content": "请分析以下身体成分数据：\n"
                    + json.dumps(
                        report_data,
                        default=str,  # Decimal 保留精确文本，不经浮点数转换。
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "body_composition_report",
                    "strict": True,
                    "schema": (
                        _ModelAnalysis.model_json_schema()
                        if self._strict_output_validation
                        else _LiteModelAnalysis.model_json_schema()
                    ),
                },
            },
            "temperature": self._temperature,
            "max_completion_tokens": self._max_completion_tokens,
            "enable_thinking": self._enable_thinking,
        }


_METRIC_NAMES = {
    "height_cm": "身高",
    "age_years": "实际年龄",
    "weight_kg": "体重",
    "total_body_water_kg": "总水分",
    "body_fat_mass_kg": "脂肪量",
    "protein_mass_kg": "蛋白质量",
    "mineral_mass_kg": "无机盐量",
    "fat_free_mass_kg": "去脂体重",
    "muscle_mass_kg": "肌肉量",
    "bone_mass_kg": "骨量",
    "skeletal_muscle_mass_kg": "骨骼肌量",
    "intracellular_water_kg": "细胞内水分",
    "extracellular_water_kg": "细胞外水分",
    "body_cell_mass_kg": "身体细胞量",
    "subcutaneous_fat_mass_kg": "皮下脂肪量",
    "body_score": "身体得分",
    "body_age": "身体年龄",
    "skeletal_muscle_index": "骨骼肌指数",
    "waist_hip_ratio": "腰臀比",
    "visceral_fat_level": "内脏脂肪等级",
    "obesity_degree_percent": "肥胖度",
    "bmi": "BMI",
    "body_fat_rate_percent": "体脂率",
    "basal_metabolism_kcal": "基础代谢",
    "recommended_calorie_intake_kcal": "推荐摄入量",
    "ideal_body_weight_kg": "理想体重",
    "target_weight_kg": "目标体重",
    "weight_control_kg": "体重控制量",
    "muscle_control_kg": "肌肉控制量",
    "fat_control_kg": "脂肪控制量",
    "subcutaneous_fat_rate_percent": "皮下脂肪率",
    "fat_mass_kg": "脂肪量",
    "fat_rate_percent": "体脂率",
    "muscle_rate_percent": "肌肉率",
}
_SEGMENT_NAMES = {
    "right_arm": "右臂",
    "left_arm": "左臂",
    "trunk": "躯干",
    "right_leg": "右腿",
    "left_leg": "左腿",
}
_EXERCISE_NAMES = {
    "walking": "步行",
    "golf": "高尔夫",
    "gateball": "门球",
    "tennis_cycling_basketball": "网球/自行车/篮球",
    "squash_shuttlecock_taekwondo_fencing": "壁球/毽子/跆拳道/击剑",
    "climbing": "爬山",
    "swimming_aerobics_jogging_football_jumping_rope": "游泳/有氧操/慢跑/足球/跳绳",
    "badminton_table_tennis": "羽毛球/乒乓球",
}


def _metric_name(path: tuple[str, ...]) -> str:
    """根据数据路径解析指标的中文名称。

    节段成分、生物电阻抗、运动消耗三类走特殊拼接规则，
    其余路径回退到通用指标名称表。
    """
    if path[0] == "segmental_composition":
        return _SEGMENT_NAMES[path[-1]] + _METRIC_NAMES[path[1]]
    if path[0] == "bioelectrical_impedance":
        return _SEGMENT_NAMES.get(path[-1], f"扩展项「{path[-1]}」") + "阻抗"
    if path[0] == "exercise_calories_kcal_per_30_min":
        return _EXERCISE_NAMES[path[-1]] + "消耗参考"
    return _METRIC_NAMES[path[-1]]


def _reference_data(request: ReportRequest) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """提取带数值的指标并生成引用编号目录。

    递归遍历请求数据，为每个具有 Decimal 数值的节点绑定
    reference_id 与中文标签，返回带编号的数据副本和编号目录。
    """
    data = request.model_dump(exclude={"measurement_id", "measured_at"}, exclude_none=True)
    catalog: dict[str, dict[str, Any]] = {}

    def visit(node: dict[str, Any], path: tuple[str, ...]) -> None:
        """递归遍历节点，为带数值的叶子指标生成引用编号。"""
        if isinstance(node.get("value"), Decimal):
            if path == ("assessment", "recommended_calorie_intake_kcal") and node["value"] == 0:
                return
            # 编号绑定完整键，扩展名称中的点号、连字符或中文不再充当路径语法。
            reference_id = f"m{len(catalog)}"
            node["reference_id"] = reference_id
            node["reference_label"] = _metric_name(path)
            catalog[reference_id] = node
            return
        for key, value in node.items():
            if isinstance(value, dict):
                visit(value, (*path, key))

    visit(data, ())
    return data, catalog


_CHINESE_DIGITS = "零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億"
_LABEL_PATTERN = "|".join(
    re.escape(label)
    for label in sorted(
        {*_METRIC_NAMES.values(), "身体评分", "体质指数", "脂肪率", "体脂量", "标准体重"},
        key=len,
        reverse=True,
    )
)
_NUMBER_UNITS = (
    r"千克|公斤|kg|厘米|cm|岁|年|级|大卡|kcal|次|回|组|遍|轮|餐|倍|"
    r"分钟|小时|秒|周|天|个月|月|毫克|克|毫升|升|斤|磅|米|个|片|粒|份|成(?!不变)|%|％"
)
_LITERAL_NUMBER = re.compile(
    rf"\d|百分之|[{_CHINESE_DIGITS}]+[点點][{_CHINESE_DIGITS}]+|"
    rf"[{_CHINESE_DIGITS}]{{2,}}|"
    rf"[{_CHINESE_DIGITS}]+\s*(?:{_NUMBER_UNITS})|"
    rf"(?:{_LABEL_PATTERN})\s*(?:的)?(?:数值|约为|为|是|约|达到|等于)?\s*[:：=]?\s*"
    rf"[{_CHINESE_DIGITS}]+分?(?=$|[，。；、！？\s]|偏高|偏低|正常|过高|过低)|"
    rf"[{_CHINESE_DIGITS}]+分(?=$|[，。；、！？\s])",
    re.IGNORECASE,
)
_NON_NUMERIC_PHRASES = re.compile(
    rf"(?<![{_CHINESE_DIGITS}])(?:万一|一一)(?![{_CHINESE_DIGITS}])|千万(?=不要|不能|别)"
)


def resolve_metric_references(
    request: ReportRequest,
    analysis: _ModelAnalysis,
    *,
    drop_invalid: bool = False,
    dropped: list[dict[str, str]] | None = None,
) -> ReportAnalysis:
    """校验模型输出并渲染参考测量句。

    确认模型文本不含未验证的数字、引用编号唯一且可用，
    随后为被引用的指标补出“参考测量”语句并组装最终报告。
    drop_invalid 为真时仅丢弃校验失败的单条建议并记录原因，
    整体分析或结构级校验失败仍会抛出。
    """
    _, catalog = _reference_data(request)

    def render(block: _ReferencedText, field: str) -> str:
        """校验单个文本块，为其中的引用指标附加参考测量句。"""
        text = block.text
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _ModelOutputError(
                "Model output contains invalid text encoding",
                reason="invalid_text_encoding",
                field=f"{field}.text",
            ) from exc
        plain_text = _NON_NUMERIC_PHRASES.sub("", text)
        if re.search(r"[{}%％+＋−-]", text):
            raise _ModelOutputError(
                "Model output contains an unverified numeric reference",
                reason="forbidden_symbol",
                field=f"{field}.text",
            )
        if _LITERAL_NUMBER.search(plain_text):
            raise _ModelOutputError(
                "Model output contains an unverified numeric reference",
                reason="literal_number",
                field=f"{field}.text",
            )
        if len(block.references) != len(set(block.references)):
            raise _ModelOutputError(
                "Model references must be unique",
                reason="duplicate_reference",
                field=f"{field}.references",
            )
        facts = []
        for index, reference_id in enumerate(block.references):
            if reference_id not in catalog:
                raise _ModelOutputError(
                    "Model referenced an unavailable metric",
                    reason="unknown_reference",
                    field=f"{field}.references[{index}]",
                )
            metric = catalog[reference_id]
            value = format(metric["value"], "f")
            if "." in value:
                value = value.rstrip("0").rstrip(".")
            facts.append(f"{metric['reference_label']} {value}{metric.get('unit') or ''}")
        if not facts:
            return text
        # 数值单独成句且自带名称，不能被前文的标签顺序、部位或单位重新绑定。
        separator = "" if text.endswith(("。", "！", "？", ".", "!", "?")) else "。"
        return f"{text}{separator}参考测量：{'；'.join(facts)}。"

    def record_drop(reason: str, field: str, validation_type: str) -> None:
        """降级模式下记录被丢弃单条建议的受控诊断信息。"""
        if dropped is not None:
            dropped.append({"reason": reason, "field": field, "validation_type": validation_type})

    overall = render(analysis.overall_assessment, "overall_assessment")
    kept: list[tuple[str, str]] = []
    for index, item in enumerate(analysis.interventions):
        field = f"interventions[{index}].content"
        try:
            content = render(item.content, field)
        except _ModelOutputError as exc:
            if not drop_invalid:
                raise
            record_drop(exc.reason, exc.field, "none")
            continue
        kept.append((item.category, content))

    while True:
        try:
            return ReportAnalysis.model_validate(
                {
                    "overall_assessment": overall,
                    "interventions": [
                        {"category": category, "content": content} for category, content in kept
                    ],
                }
            )
        except ValidationError as exc:
            if not drop_invalid:
                raise
            # 追加参考句后超出公开长度上限的单条建议在降级模式下单独丢弃。
            error = exc.errors()[0]
            location = error["loc"]
            if (
                location[:1] == ("interventions",)
                and len(location) == 3
                and isinstance(location[1], int)
                and location[2] == "content"
                and error["type"] == "string_too_long"
            ):
                index = location[1]
                record_drop(
                    "rendered_text_too_long",
                    f"interventions[{index}].content",
                    "string_too_long",
                )
                kept.pop(index)
                continue
            raise
