"""身体成分报告接口的请求与响应 Schema。"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


def _reject_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("booleans are not valid numeric values or levels")
    return value


# 技术性位数上限，不代表医学阈值或设备的小数精度。
MetricNumber = Annotated[
    Decimal,
    Field(allow_inf_nan=False, max_digits=12),
    BeforeValidator(_reject_bool),
]
MetricLevel = Annotated[int | str, BeforeValidator(_reject_bool)]


class ReportSchema(BaseModel):
    """报告相关 Schema 基类。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class ReferenceMetric(ReportSchema):
    """统一指标对象：值、单位、等级和可选标准区间。"""

    value: MetricNumber | None = None
    unit: str | None = None
    level: MetricLevel | None = None
    standard_min: MetricNumber | None = None
    standard_max: MetricNumber | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_scalar(cls, data: object) -> object:
        """兼容旧版裸数值，同时允许完整指标对象。"""
        if data is None:
            return {}
        if isinstance(data, (cls, Mapping)):
            return data
        return {"value": data}

    @model_validator(mode="after")
    def validate_standard_range(self) -> "ReferenceMetric":
        if (
            self.standard_min is not None
            and self.standard_max is not None
            and self.standard_min > self.standard_max
        ):
            raise ValueError("standard_min must not exceed standard_max")
        return self


def _validate_metric(
    metric: ReferenceMetric,
    field_name: str,
    units: tuple[str, ...] | None = None,
) -> ReferenceMetric:
    if units is None:
        if field_name.endswith("_kg"):
            units = ("kg",)
        elif field_name.endswith("_percent"):
            units = ("%", "％")
        elif field_name.endswith("_kcal"):
            units = ("kCal", "kcal", "kCal/day", "kcal/day")
        else:
            units = {
                "height_cm": ("cm",),
                "age_years": ("岁", "year", "years"),
                "body_age": ("岁", "year", "years"),
                "bmi": ("kg/m2", "kg/m²"),
                "waist_hip_ratio": (),
                "body_score": ("分",),
                "visceral_fat_level": ("级",),
            }.get(field_name)
    if units is not None and metric.unit not in (None, "", *units):
        raise ValueError(f"{field_name} has an incompatible unit")
    if not field_name.endswith("_control_kg"):
        for name in ("value", "standard_min", "standard_max"):
            value = getattr(metric, name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name}.{name} must be non-negative")
    if (
        field_name in {"height_cm", "weight_kg", "ideal_body_weight_kg", "target_weight_kg"}
        and metric.value is not None
        and metric.value <= 0
    ):
        raise ValueError(f"{field_name}.value must be positive")
    return metric


class BodyTypeValue(ReportSchema):
    """身体类型判定：协议 0x01-0x09 编码及可选中文名称。"""

    code: int | None = Field(default=None, ge=1, le=9, strict=True)
    name: str | None = Field(default=None, max_length=100)
    level: MetricLevel | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_code(cls, data: object) -> object:
        if data is None:
            return {}
        if isinstance(data, (cls, Mapping)):
            return data
        if type(data) is int:
            return {"code": data}
        if isinstance(data, str):
            return {"name": data}
        raise ValueError("body type must be an integer code, name, or object")

    @model_validator(mode="after")
    def validate_code_name(self) -> "BodyTypeValue":
        names = {
            1: "偏瘦型",
            2: "偏瘦肌肉型",
            3: "肌肉发达型",
            4: "浮肿肥胖型",
            5: "偏胖肌肉型",
            6: "肌肉型偏胖",
            7: "缺乏运动型",
            8: "标准型",
            9: "标准肌肉型",
        }
        # 仅核对同时提供的协议编码和名称，不替调用方补全缺省字段。
        if self.code is not None and self.name is not None and self.name != names[self.code]:
            raise ValueError("body type code and name must match")
        return self


class SubjectProfile(ReportSchema):
    """计算 BMI 和八电极算法所需的基础资料。"""

    sex: Literal["female", "male"] | None = None
    height_cm: ReferenceMetric = Field(default_factory=ReferenceMetric)
    age_years: ReferenceMetric = Field(default_factory=ReferenceMetric)

    @field_validator("height_cm", "age_years")
    @classmethod
    def validate_metrics(cls, metric: ReferenceMetric, info: ValidationInfo) -> ReferenceMetric:
        return _validate_metric(metric, info.field_name)


class BodyComposition(ReportSchema):
    """第一包：全身体组成参数。"""

    weight_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    total_body_water_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    body_fat_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    protein_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    mineral_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    fat_free_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    muscle_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    bone_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    skeletal_muscle_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    intracellular_water_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    extracellular_water_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    body_cell_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    subcutaneous_fat_mass_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)

    @field_validator("*")
    @classmethod
    def validate_metrics(cls, metric: ReferenceMetric, info: ValidationInfo) -> ReferenceMetric:
        return _validate_metric(metric, info.field_name)


class BioelectricalImpedance(ReportSchema):
    """生物电阻抗数据包，允许设备协议扩展字段。"""

    __pydantic_extra__: dict[str, ReferenceMetric] = Field(init=False)

    right_arm: ReferenceMetric = Field(default_factory=ReferenceMetric)
    left_arm: ReferenceMetric = Field(default_factory=ReferenceMetric)
    trunk: ReferenceMetric = Field(default_factory=ReferenceMetric)
    right_leg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    left_leg: ReferenceMetric = Field(default_factory=ReferenceMetric)

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    @field_validator("*")
    @classmethod
    def validate_metrics(cls, metric: ReferenceMetric, info: ValidationInfo) -> ReferenceMetric:
        return _validate_metric(metric, info.field_name, ("ohm", "Ω"))


class SegmentValues(ReportSchema):
    """第二包：右上臂、左上臂、躯干、右腿、左腿的节段指标。"""

    right_arm: ReferenceMetric = Field(default_factory=ReferenceMetric)
    left_arm: ReferenceMetric = Field(default_factory=ReferenceMetric)
    trunk: ReferenceMetric = Field(default_factory=ReferenceMetric)
    right_leg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    left_leg: ReferenceMetric = Field(default_factory=ReferenceMetric)

    @field_validator("*")
    @classmethod
    def validate_metrics(cls, metric: ReferenceMetric, info: ValidationInfo) -> ReferenceMetric:
        return _validate_metric(metric, info.field_name)


class SegmentalComposition(ReportSchema):
    """第二包：节段脂肪和肌肉信息。"""

    fat_mass_kg: SegmentValues = Field(default_factory=SegmentValues)
    fat_rate_percent: SegmentValues = Field(default_factory=SegmentValues)
    muscle_mass_kg: SegmentValues = Field(default_factory=SegmentValues)
    muscle_rate_percent: SegmentValues = Field(default_factory=SegmentValues)

    @field_validator("*")
    @classmethod
    def validate_units(cls, segments: SegmentValues, info: ValidationInfo) -> SegmentValues:
        for name in type(segments).model_fields:
            _validate_metric(getattr(segments, name), info.field_name)
        return segments


class Assessment(ReportSchema):
    """第三包：评价建议。"""

    body_score: ReferenceMetric = Field(default_factory=ReferenceMetric)
    body_age: ReferenceMetric = Field(default_factory=ReferenceMetric)
    body_type: BodyTypeValue = Field(default_factory=BodyTypeValue)
    skeletal_muscle_index: ReferenceMetric = Field(default_factory=ReferenceMetric)
    waist_hip_ratio: ReferenceMetric = Field(default_factory=ReferenceMetric)
    visceral_fat_level: ReferenceMetric = Field(default_factory=ReferenceMetric)
    obesity_degree_percent: ReferenceMetric = Field(default_factory=ReferenceMetric)
    bmi: ReferenceMetric = Field(default_factory=ReferenceMetric)
    body_fat_rate_percent: ReferenceMetric = Field(default_factory=ReferenceMetric)
    basal_metabolism_kcal: ReferenceMetric = Field(default_factory=ReferenceMetric)
    recommended_calorie_intake_kcal: ReferenceMetric = Field(default_factory=ReferenceMetric)
    ideal_body_weight_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    target_weight_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    weight_control_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    muscle_control_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    fat_control_kg: ReferenceMetric = Field(default_factory=ReferenceMetric)
    subcutaneous_fat_rate_percent: ReferenceMetric = Field(default_factory=ReferenceMetric)

    @field_validator("*")
    @classmethod
    def validate_metrics(
        cls, metric: ReferenceMetric | BodyTypeValue, info: ValidationInfo
    ) -> ReferenceMetric | BodyTypeValue:
        if isinstance(metric, ReferenceMetric):
            return _validate_metric(metric, info.field_name)
        return metric


class ExerciseCalories(ReportSchema):
    """第四包：每 30 分钟各类运动的消耗热量。"""

    walking: ReferenceMetric = Field(default_factory=ReferenceMetric)
    golf: ReferenceMetric = Field(default_factory=ReferenceMetric)
    gateball: ReferenceMetric = Field(default_factory=ReferenceMetric)
    tennis_cycling_basketball: ReferenceMetric = Field(default_factory=ReferenceMetric)
    squash_shuttlecock_taekwondo_fencing: ReferenceMetric = Field(default_factory=ReferenceMetric)
    climbing: ReferenceMetric = Field(default_factory=ReferenceMetric)
    swimming_aerobics_jogging_football_jumping_rope: ReferenceMetric = Field(
        default_factory=ReferenceMetric
    )
    badminton_table_tennis: ReferenceMetric = Field(default_factory=ReferenceMetric)

    @field_validator("*")
    @classmethod
    def validate_metrics(cls, metric: ReferenceMetric, info: ValidationInfo) -> ReferenceMetric:
        return _validate_metric(metric, info.field_name, ("kCal/30min", "kcal/30min"))


class SegmentalStandards(ReportSchema):
    """第五包的节段评级：0 低标准、1 标准、2 超标准。"""

    right_arm: Literal[0, 1, 2] | None = None
    left_arm: Literal[0, 1, 2] | None = None
    trunk: Literal[0, 1, 2] | None = None
    right_leg: Literal[0, 1, 2] | None = None
    left_leg: Literal[0, 1, 2] | None = None

    @field_validator("*", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("segment standard must be an integer from 0 to 2")
        return value


class SegmentStandards(ReportSchema):
    """第五包：节段脂肪和肌肉评级。"""

    fat: SegmentalStandards = Field(default_factory=SegmentalStandards)
    muscle: SegmentalStandards = Field(default_factory=SegmentalStandards)


class ReportRequest(ReportSchema):
    """报告生成请求：所有测量指标均可缺省。"""

    measurement_id: str | None = Field(default=None, max_length=128)
    measured_at: datetime | None = None
    subject: SubjectProfile = Field(default_factory=SubjectProfile)
    bioelectrical_impedance: BioelectricalImpedance = Field(default_factory=BioelectricalImpedance)
    body_composition: BodyComposition = Field(default_factory=BodyComposition)
    segmental_composition: SegmentalComposition = Field(default_factory=SegmentalComposition)
    assessment: Assessment = Field(default_factory=Assessment)
    exercise_calories_kcal_per_30_min: ExerciseCalories = Field(default_factory=ExerciseCalories)
    segment_standards: SegmentStandards = Field(default_factory=SegmentStandards)


class Intervention(ReportSchema):
    """单条干预建议。"""

    category: Literal[
        "direction",
        "training",
        "nutrition",
        "weight",
        "retest",
        "body_status",
    ]
    content: str = Field(min_length=1, max_length=500)


class ReportAnalysis(ReportSchema):
    """模型输出的分析结果。"""

    overall_assessment: str = Field(min_length=1, max_length=800)
    interventions: list[Intervention] = Field(max_length=6)

    @field_validator("interventions")
    @classmethod
    def unique_categories(cls, interventions: list[Intervention]) -> list[Intervention]:
        categories = [item.category for item in interventions]
        if len(categories) != len(set(categories)):
            raise ValueError("intervention categories must be unique")
        return interventions


class ReportResponse(BaseModel):
    """报告接口响应体。"""

    request_id: str
    status: Literal["completed"] = "completed"
    generation_mode: Literal["model", "fallback"]
    overall_assessment: str
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    interventions: list[Intervention]
