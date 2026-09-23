# Report 接口设计

## 1. 接口概述

`POST /api/v1/report` 接收 BMH05108 协议“八电极 Body270 算法输出（0xD0）”解码后的全部业务指标，生成身体成分分析报告。

协议响应由五个数据包组成：

| 数据包 | 内容 | 指标数量 |
| --- | --- | ---: |
| 第一包 | 全身体组成参数 | 13 |
| 第二包 | 节段脂肪、肌肉信息 | 20 |
| 第三包 | 评价建议 | 17 |
| 第四包 | 运动消耗量 | 8 |
| 第五包 | 节段脂肪、肌肉标准等级 | 10 |

此外，报告需要基础资料中的性别、身高和年龄。体重来自第一包，BMI 来自第三包。

### 1.1 模块职责

| 模块 | 职责 |
| --- | --- |
| 设备解包层 | 将 0xD0 响应包转换为 API 请求字段 |
| 后端确定性分析 | 生成 `key_evidence` |
| Qwen Flash | 生成 `overall_assessment` 和 `interventions` |
| 规则兜底 | 模型未配置、失败或超时时生成基础干预建议 |
| 前端 | 展示接口响应 |

模型不生成、不修改 `key_evidence`。协议帧头、帧长度、命令号、包号、错误类型和校验位不属于报告分析入参。

## 2. 请求

### 2.1 基本信息

```http
POST /api/v1/report
Content-Type: application/json
X-User-Id: user-123
```

`X-User-Id` 非必填，未提供时按匿名用户处理，不会发送给模型供应商。

### 2.2 请求原则

- 所有字段均非必填。
- 五个数据包对象均可省略。
- 每个指标对象中的字段也均可省略。
- 接口只分析实际提供的指标，不为缺失指标补默认值。
- 指标对象推荐使用 `value`、`unit`、`level`、`standard_min`、`standard_max`。
- 对于协议没有标准区间的字段，只提供 `value` 和 `unit`。
- 未知字段返回 `422`，防止设备字段拼写错误被静默忽略；`bioelectrical_impedance` 允许设备扩展指标名，扩展值仍须遵守指标对象契约，其内部未知字段也返回 `422`。
- `key_evidence` 不属于请求字段，由后端确定性模块在服务端生成。

最小请求：

```json
{}
```

### 2.3 指标对象

带区间的指标：

```json
{
  "value": 62.3,
  "unit": "kg",
  "level": "normal",
  "standard_min": 55.3,
  "standard_max": 74.9
}
```

没有标准区间的指标：

```json
{
  "value": 65.1,
  "unit": "kg"
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `value` | number | 当前值 |
| `unit` | string | 单位；由协议字段决定，建议一并传入 |
| `level` | string/integer | 指标等级或状态，例如 `low`、`normal`、`high`；没有等级时可省略，不接受布尔值 |
| `standard_min` | number | 标准下限 |
| `standard_max` | number | 标准上限 |

当前 Schema 兼容旧版裸数值，例如 `"muscle_control_kg": 7.3`，但新接入建议使用完整指标对象。

数值及标准上下限必须有限，十进制总位数最多 12 位（技术边界，不是医学阈值）。
按十进制系数及指数检查边界，不依赖 Decimal context 的舍入或下溢；忽略无意义尾零并规范零，
仍支持合法科学计数法，例如 `"1e-12"`、`"1.2300"`；`"1e-2147483648"` 等超界非零值返回 `422`，不会悄悄变成零。
同时提供区间两端时，下限不得大于上限。三个评价控制量允许负值，其他测量指标及其区间非负，
身高、体重、理想体重和目标体重的值须大于零。布尔值不作为数值或评级接受。
单位可缺省；提供时必须匹配指标，例如体重量为 `kg`、BMI 为 `kg/m2` 或 `kg/m²`、
热量为 `kCal`/`kcal`（支持 `/day`）、运动热量为 `kCal/30min`/`kcal/30min`。
阻抗扩展指标同样要求非负，单位只允许缺省、空字符串、`ohm` 或 `Ω`；扩展键名不改变这些约束，中文、点号、连字符等合法名称继续支持。
不限制肥胖度和节段肌肉率必须小于等于百分之百，也不为骨骼肌质量指数强加固定单位。
身体类型编码和节段评级只接受对应范围内的整数，身体类型名称最多 100 字符。
身体类型同时提供 `code` 和 `name` 时必须与下表协议映射一致，否则返回 `422`；仍支持只提供编码、只提供名称或全部缺省，不自动补全。
非法输入中的裸 `NaN`、`Infinity` 等非有限数、孤立 Unicode 代理字符、非 JSON 二进制请求返回 `422`。
错误详情递归处理键和值：非有限数转文本，非法字符或字节以转义文本显示，避免错误响应自身变成 `500`；正常 `422` 结构保持不变。

## 3. 完整请求示例

下面示例覆盖五个 0xD0 数据包的全部字段，以及 BMI 计算所需的基础资料。实际业务中任何字段都可以省略。

```json
{
  "measurement_id": "measurement-20260921-0001",
  "measured_at": "2026-09-21T09:30:00+08:00",
  "subject": {
    "sex": "male",
    "height_cm": {
      "value": 172,
      "unit": "cm"
    },
    "age_years": {
      "value": 28,
      "unit": "岁"
    }
  },
  "body_composition": {
    "weight_kg": {
      "value": 62.3,
      "unit": "kg",
      "level": "normal",
      "standard_min": 55.3,
      "standard_max": 74.9
    },
    "total_body_water_kg": {
      "value": 35.2,
      "unit": "kg",
      "level": "low",
      "standard_min": 36.5,
      "standard_max": 44.7
    },
    "body_fat_mass_kg": {
      "value": 14.3,
      "unit": "kg",
      "level": "normal",
      "standard_min": 7.8,
      "standard_max": 15.6
    },
    "protein_mass_kg": {
      "value": 9.5,
      "unit": "kg",
      "level": "low",
      "standard_min": 9.8,
      "standard_max": 11.9
    },
    "mineral_mass_kg": {
      "value": 3.3,
      "unit": "kg",
      "level": "normal",
      "standard_min": 3.3,
      "standard_max": 4.1
    },
    "fat_free_mass_kg": {
      "value": 48.0,
      "unit": "kg",
      "level": "normal",
      "standard_min": 47.5,
      "standard_max": 59.3
    },
    "muscle_mass_kg": {
      "value": 44.7,
      "unit": "kg",
      "level": "low",
      "standard_min": 47.0,
      "standard_max": 64.6
    },
    "bone_mass_kg": {
      "value": 2.6,
      "unit": "kg",
      "level": "low",
      "standard_min": 2.8,
      "standard_max": 3.5
    },
    "skeletal_muscle_mass_kg": {
      "value": 26.4,
      "unit": "kg",
      "level": "low",
      "standard_min": 27.8,
      "standard_max": 34.0
    },
    "intracellular_water_kg": {
      "value": 22.0,
      "unit": "kg",
      "level": "low",
      "standard_min": 22.7,
      "standard_max": 27.7
    },
    "extracellular_water_kg": {
      "value": 13.1,
      "unit": "kg",
      "level": "low",
      "standard_min": 13.9,
      "standard_max": 17.0
    },
    "body_cell_mass_kg": {
      "value": 31.6,
      "unit": "kg",
      "level": "low",
      "standard_min": 32.5,
      "standard_max": 39.7
    },
    "subcutaneous_fat_mass_kg": {
      "value": 12.9,
      "unit": "kg"
    }
  },
  "segmental_composition": {
    "fat_mass_kg": {
      "right_arm": {"value": 0.8, "unit": "kg"},
      "left_arm": {"value": 0.9, "unit": "kg"},
      "trunk": {"value": 7.2, "unit": "kg"},
      "right_leg": {"value": 1.8, "unit": "kg"},
      "left_leg": {"value": 1.9, "unit": "kg"}
    },
    "fat_rate_percent": {
      "right_arm": {"value": 12.4, "unit": "%", "level": "normal"},
      "left_arm": {"value": 12.8, "unit": "%", "level": "normal"},
      "trunk": {"value": 25.6, "unit": "%", "level": "high"},
      "right_leg": {"value": 20.1, "unit": "%", "level": "normal"},
      "left_leg": {"value": 20.6, "unit": "%", "level": "normal"}
    },
    "muscle_mass_kg": {
      "right_arm": {"value": 2.3, "unit": "kg"},
      "left_arm": {"value": 2.5, "unit": "kg"},
      "trunk": {"value": 21.2, "unit": "kg"},
      "right_leg": {"value": 7.5, "unit": "kg"},
      "left_leg": {"value": 7.7, "unit": "kg"}
    },
    "muscle_rate_percent": {
      "right_arm": {"value": 35.2, "unit": "%", "level": "low"},
      "left_arm": {"value": 35.6, "unit": "%", "level": "low"},
      "trunk": {"value": 47.8, "unit": "%", "level": "normal"},
      "right_leg": {"value": 37.9, "unit": "%", "level": "low"},
      "left_leg": {"value": 38.3, "unit": "%", "level": "low"}
    }
  },
  "assessment": {
    "body_score": {
      "value": 66,
      "unit": "分"
    },
    "body_age": {
      "value": 19,
      "unit": "岁"
    },
    "body_type": {
      "code": 4,
      "name": "浮肿肥胖型"
    },
    "skeletal_muscle_index": {
      "value": 67,
      "unit": null
    },
    "waist_hip_ratio": {
      "value": 0.79,
      "unit": null,
      "level": "low",
      "standard_min": 0.80,
      "standard_max": 0.90
    },
    "visceral_fat_level": {
      "value": 5,
      "unit": "级",
      "level": "normal",
      "standard_min": 1,
      "standard_max": 9
    },
    "obesity_degree_percent": {
      "value": 95,
      "unit": "%",
      "level": "normal",
      "standard_min": 90,
      "standard_max": 110
    },
    "bmi": {
      "value": 21.1,
      "unit": "kg/m2",
      "level": "normal",
      "standard_min": 18.5,
      "standard_max": 23.0
    },
    "body_fat_rate_percent": {
      "value": 22.9,
      "unit": "%",
      "level": "high",
      "standard_min": 10.0,
      "standard_max": 20.0
    },
    "basal_metabolism_kcal": {
      "value": 1406,
      "unit": "kCal",
      "level": "normal",
      "standard_min": 1399,
      "standard_max": 1628
    },
    "recommended_calorie_intake_kcal": {
      "value": 1827,
      "unit": "kCal"
    },
    "ideal_body_weight_kg": {
      "value": 65.1,
      "unit": "kg"
    },
    "target_weight_kg": {
      "value": 65.1,
      "unit": "kg"
    },
    "weight_control_kg": {
      "value": 2.8,
      "unit": "kg"
    },
    "muscle_control_kg": {
      "value": 7.3,
      "unit": "kg"
    },
    "fat_control_kg": {
      "value": -4.5,
      "unit": "kg"
    },
    "subcutaneous_fat_rate_percent": {
      "value": 20.7,
      "unit": "%",
      "level": "high",
      "standard_min": 8.6,
      "standard_max": 16.7
    }
  },
  "exercise_calories_kcal_per_30_min": {
    "walking": {"value": 124, "unit": "kCal/30min"},
    "golf": {"value": 109, "unit": "kCal/30min"},
    "gateball": {"value": 118, "unit": "kCal/30min"},
    "tennis_cycling_basketball": {"value": 186, "unit": "kCal/30min"},
    "squash_shuttlecock_taekwondo_fencing": {
      "value": 311,
      "unit": "kCal/30min"
    },
    "climbing": {"value": 203, "unit": "kCal/30min"},
    "swimming_aerobics_jogging_football_jumping_rope": {
      "value": 217,
      "unit": "kCal/30min"
    },
    "badminton_table_tennis": {"value": 140, "unit": "kCal/30min"}
  },
  "segment_standards": {
    "fat": {
      "right_arm": 1,
      "left_arm": 1,
      "trunk": 2,
      "right_leg": 1,
      "left_leg": 1
    },
    "muscle": {
      "right_arm": 0,
      "left_arm": 0,
      "trunk": 1,
      "right_leg": 0,
      "left_leg": 0
    }
  }
}
```

## 4. 字段定义

### 4.1 `subject`

| 字段 | 类型 | 单位/取值 | 说明 |
| --- | --- | --- | --- |
| `sex` | string | `female` 或 `male` | 性别 |
| `height_cm` | metric | cm | 身高；BMI 解释需要 |
| `age_years` | metric | 岁 | 年龄；身体年龄分析需要 |

### 4.2 `body_composition`：第一包，13 项

| API 字段 | 协议指标 | 单位 | 标准区间 |
| --- | --- | --- | --- |
| `weight_kg` | 体重量 | kg | 有 |
| `total_body_water_kg` | 水分量 | kg | 有 |
| `body_fat_mass_kg` | 体脂量 | kg | 有 |
| `protein_mass_kg` | 蛋白质量 | kg | 有 |
| `mineral_mass_kg` | 无机盐量 | kg | 有 |
| `fat_free_mass_kg` | 去脂体重 | kg | 有 |
| `muscle_mass_kg` | 肌肉量 | kg | 有 |
| `bone_mass_kg` | 骨量 | kg | 有 |
| `skeletal_muscle_mass_kg` | 骨骼肌量 | kg | 有 |
| `intracellular_water_kg` | 细胞内水量 | kg | 有 |
| `extracellular_water_kg` | 细胞外水量 | kg | 有 |
| `body_cell_mass_kg` | 身体细胞量 | kg | 有 |
| `subcutaneous_fat_mass_kg` | 皮下脂肪量 | kg | 协议未提供 |

### 4.3 `segmental_composition`：第二包，20 项

每组都包含 `right_arm`、`left_arm`、`trunk`、`right_leg`、`left_leg`。

| 分组 | 字段 | 单位 | 标准/等级来源 |
| --- | --- | --- | --- |
| `fat_mass_kg` | 五个节段脂肪量 | kg | 协议未提供区间 |
| `fat_rate_percent` | 五个节段脂肪率 | % | `segment_standards.fat` |
| `muscle_mass_kg` | 五个节段肌肉量 | kg | 协议未提供区间 |
| `muscle_rate_percent` | 五个节段肌肉率 | % | `segment_standards.muscle` |

第二包的肌肉率字段由 Master 固件 V1.3 及以上版本支持；未提供时不参与分析。

### 4.4 `assessment`：第三包，17 项

| API 字段 | 协议指标 | 单位 | 标准区间 |
| --- | --- | --- | --- |
| `body_score` | 身体得分 | 分 | 无 |
| `body_age` | 身体年龄 | 岁 | 无 |
| `body_type` | 身体类型 | 编码 + 名称 | 1-9 |
| `skeletal_muscle_index` | 骨骼肌质量指数 | 无固定单位 | 无 |
| `waist_hip_ratio` | 腰臀比 | 比值 | 有 |
| `visceral_fat_level` | 内脏脂肪等级 | 级 | 有 |
| `obesity_degree_percent` | 肥胖度 | % | 有 |
| `bmi` | BMI | kg/m2 | 有 |
| `body_fat_rate_percent` | 体脂率 | % | 有 |
| `basal_metabolism_kcal` | 基础代谢 | kCal | 有 |
| `recommended_calorie_intake_kcal` | 建议摄入量 | kCal | 无 |
| `ideal_body_weight_kg` | 理想体重 | kg | 无 |
| `target_weight_kg` | 目标体重 | kg | 无 |
| `weight_control_kg` | 体重控制量 | kg | 无 |
| `muscle_control_kg` | 肌肉控制量 | kg | 无 |
| `fat_control_kg` | 脂肪控制量 | kg | 无 |
| `subcutaneous_fat_rate_percent` | 皮下脂肪率 | % | 有 |

身体类型编码：

| 编码 | 类型 |
| ---: | --- |
| 1 | 偏瘦型 |
| 2 | 偏瘦肌肉型 |
| 3 | 肌肉发达型 |
| 4 | 浮肿肥胖型 |
| 5 | 偏胖肌肉型 |
| 6 | 肌肉型偏胖 |
| 7 | 缺乏运动型 |
| 8 | 标准型 |
| 9 | 标准肌肉型 |

控制量约定：

- 正值表示建议增加。
- 负值表示建议减少。
- `0` 表示不需要调整。

### 4.5 `exercise_calories_kcal_per_30_min`：第四包，8 项

| API 字段 | 协议指标 | 单位 |
| --- | --- | --- |
| `walking` | 步行 | kCal/30min |
| `golf` | 高尔夫 | kCal/30min |
| `gateball` | 门球 | kCal/30min |
| `tennis_cycling_basketball` | 网球/自行车/篮球 | kCal/30min |
| `squash_shuttlecock_taekwondo_fencing` | 壁球/毽子/跆拳道/击剑 | kCal/30min |
| `climbing` | 爬山 | kCal/30min |
| `swimming_aerobics_jogging_football_jumping_rope` | 游泳/有氧操/慢跑/足球/跳绳 | kCal/30min |
| `badminton_table_tennis` | 羽毛球/乒乓球 | kCal/30min |

### 4.6 `segment_standards`：第五包，10 项

`fat` 和 `muscle` 各包含五个节段字段。取值：

- `0`：低标准
- `1`：标准
- `2`：超标准

## 5. 响应

```json
{
  "request_id": "req_7f3c...",
  "status": "completed",
  "generation_mode": "model",
  "overall_assessment": "整体身体成分存在肌肉不足和脂肪偏高倾向，建议以增肌为主、减脂为辅。",
  "key_evidence": [
    {
      "tag": "肌肉不足",
      "text": "肌肉量 44.7kg，低于参考下限 47.0kg",
      "cls": "lo"
    }
  ],
  "interventions": [
    {
      "category": "direction",
      "content": "以增肌为核心，减脂同步进行"
    }
  ]
}
```

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | string | 请求唯一标识 |
| `status` | string | 当前固定为 `completed` |
| `generation_mode` | string | `model` 或 `fallback` |
| `overall_assessment` | string | 模型或兜底生成的整体分析 |
| `key_evidence` | array | 后端确定性模块生成；没有结果时为 `[]` |
| `interventions` | array | 按固定顺序返回方向、训练、营养、体重、复测建议、身体状态六类；每类优先使用模型结果，模型缺失该类时由后端单独补充对应兜底 |

`key_evidence` 不由请求方传入，也不发送给模型。

## 6. 模型与规则兜底

### 6.1 模型正常

`generation_mode=model`：

- `overall_assessment` 来自模型。
- `interventions` 来自模型。
- `key_evidence` 来自后端。

当前提示词为 `report-v7`，沿用结构化引用：`overall_assessment` 和每条干预的 `content` 均为
`{"text":"完整的定性说明。","references":["m0"]}`。对外响应仍为普通字符串，
自定义 `ReportAnalyzer` 的 `ReportAnalysis` 契约也不变。
提示词要求有身体实测值时生成复测条件和跟踪重点，无需历史报告，不编造复测周期；
仅有部分指标时仍可生成复测建议。提示词以反例禁止中文数值复述（如“身体得分为六十六分”）。
模型漏类时继续由后端补齐，兜底规则不变。

每个可引用指标在模型输入中携带本次请求的 `reference_id` 和后端生成的 `reference_label`。
数值以精确十进制字符串发送，不经浮点转换。模型仅选择编号，不提供或修改引用的名称、部位、数值、符号和单位。
后端在独立的“参考测量”句中生成完整名称及原始数值、单位；交换引用顺序不会交换指标名称与数值。
扩展阻抗键名中的中文、连字符、点号均原样保留，不再作为路径语法解析。

缺少 `value` 的指标和零推荐摄入量不分配引用编号。未知编号、重复编号、旧式占位符、直接数字、
常见中文数值（含中文小数、成数、训练次数）会使对应文本块校验失败：整体分析失败时整份走规则兜底，
单条建议失败时仅丢弃该条、按缺类由后端补齐，其余模型建议保留；重复分类仍触发整份兜底。
最多接受六个互不重复的分类。
回填后的文本仍受整体分析 800 字符、单条建议 500 字符限制。
定性说明中的正常并列指标及同义名称不再与后续数值做相邻词匹配，不会因此误触发兜底。
这些约束保证引用的名值绑定，不代表能够验证全部自然语言表达、异常结论或医学质量。

### 6.2 模型不可用、失败或超时

`generation_mode=fallback`，基础规则并行判断，所有命中项都返回：

1. `muscle_control_kg > 0`：循序渐进进行抗阻训练以支持增肌。
2. `fat_control_kg < 0`：结合运动耐受情况安排有氧活动以支持减脂；两项控制量均为零时，不新增增肌或减脂目标，不输出固定训练频率和时长。
3. `ideal_body_weight_kg` 和非零 `weight_control_kg` 均已提供：保留控制量正负号，明确增加或减少体重；控制量为零时说明无需据此调整。
4. `recommended_calorie_intake_kcal > 0`：每日摄入参考；零值仍允许输入，但按营养参考不足处理，不作为摄入目标。

身体状态兜底仅在指标有 `value` 时判断异常：非空 `level` 优先，缺省或空等级才使用输入标准区间，边界值不视为异常；未知或数字等级不猜测其含义。
判断覆盖人体成分、BMI/体脂率/内脏脂肪等评价和节段数据；节段比例指标无自身等级和区间时，才参考对应的 `segment_standards`。
身体年龄比较予以保留，但不会仅凭身体年龄较小推断整体健康或建议保持现状。

模型正常但未返回某一分类时，只对该分类使用规则兜底，其余分类仍保留模型结果。
单条建议因数字、符号、引用或长度校验失败被丢弃时，同样只对该分类补齐规则内容，`generation_mode` 仍为 `model`。
模型整体不可用、整体分析失败、结构校验失败或超时时，六个分类全部使用规则兜底；缺少对应参考数据的分类会返回
“暂无足够相关参考数据”的说明。

## 7. 错误处理

| HTTP 状态码 | 场景 |
| --- | --- |
| `200` | 请求成功，包括模型分析和规则兜底 |
| `422` | 字段类型错误、未知字段或数值不符合约束 |
| `500` | 服务内部未处理异常 |

模型调用失败不直接返回 `5xx`，而是返回 `200` 和 `generation_mode=fallback`。

## 8. 模型配置

```dotenv
DASHSCOPE_API_KEY=sk-...
WEIGHT_AGENT_REPORT_MODEL=qwen3.8-flash
WEIGHT_AGENT_REPORT_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
WEIGHT_AGENT_REPORT_MODEL_TIMEOUT_SECONDS=20
WEIGHT_AGENT_REPORT_MODEL_TEMPERATURE=0
WEIGHT_AGENT_REPORT_MODEL_MAX_COMPLETION_TOKENS=800
WEIGHT_AGENT_REPORT_MODEL_ENABLE_THINKING=false
WEIGHT_AGENT_REPORT_MODEL_STRICT_OUTPUT_VALIDATION=false
```

其中 `temperature`、最大输出 token 数和 thinking 开关均可直接调整，无需修改代码。
`DASHSCOPE_API_KEY` 缺失、为空或仅包含空白时均视为未配置，直接使用规则兜底，不调用模型。

发送给模型的内容不包含 `X-User-Id`、`measurement_id` 或 `measured_at`。`key_evidence` 由后端生成，也不会发送给模型。

## 9. 性能日志

报告请求完成后，服务输出一条 `INFO` 级别的 `report_request_completed` 日志。
控制台还会显示模型调用和最终来源，便于确认本次请求是否真正调用模型：

```text
[REPORT_MODEL] calling model=qwen3.8-flash
[REPORT_RESULT] source=模型生成 model_called=True model=qwen3.8-flash
  model_categories=direction,training,nutrition,weight,retest,body_status
  fallback_categories=none duration_ms=9650.17
```

如果模型只生成部分分类，示例为：

```text
[REPORT_RESULT] source=模型生成 model_called=True model=qwen3.8-flash
  model_categories=training,body_status
  fallback_categories=direction,nutrition,weight,retest duration_ms=1200.45
```

模型调用失败或超时时：

```text
[REPORT_RESULT] source=规则兜底 model_called=True model=qwen3.8-flash
  model_categories=none
  fallback_categories=direction,training,nutrition,weight,retest,body_status
  duration_ms=45012.31
```

日志字段：

| 字段 | 说明 |
| --- | --- |
| `request_id` | 本次请求唯一标识 |
| `generation_mode` | `model` 或 `fallback` |
| `model` | 使用的模型名称；未配置时为空 |
| `prompt_version` | 当前提示词版本，例如 `report-v7` |
| `model_duration_ms` | 模型调用耗时 |
| `total_duration_ms` | 从工作流开始到结果生成的总耗时 |
| `fallback_reason` | 兜底原因；模型成功时为空，超时为 `timeout` |

模型分析器还会记录模型调用成功或失败的耗时日志。失败日志提供受控原因和结构字段位置，例如：

```text
report_model_failed model=qwen3.7-flash prompt_version=report-v7 duration_ms=13548
  error=_ModelOutputError stage=resolve_references reason=literal_number
  field=overall_assessment.text validation_type=none finish_reason=stop status_code=200
```

单条建议校验失败不再整份兜底，而是丢弃该条并单独记录：

```text
report_intervention_dropped model=qwen3.7-flash prompt_version=report-v7
  stage=resolve_references reason=literal_number field=interventions[2].content.text
  validation_type=none
```

以上仅为格式示例，不代表已定位某次真实请求的失败原因。`interventions[2]` 是模型返回列表的第三项，
不是后端补齐后固定顺序的第三类；`status_code` 是模型服务 HTTP 状态，不是报告生成是否成功。

| 字段 | 说明 |
| --- | --- |
| `stage` | `build_payload`、`request`、`response_json`、`response_content`、`model_schema` 或 `resolve_references` |
| `reason` | 固定失败原因码，见下表 |
| `field` | 失败字段及列表下标；根节点为 `$`，未知字段名替换为 `<extra>` |
| `validation_type` | Pydantic 错误类型，例如 `string_too_long`、`literal_error`；非 Schema 错误为 `none` |
| `finish_reason` | 仅保留 `stop`、`length`、`content_filter`、`tool_calls`、`function_call`，缺失或未知为 `unknown` |
| `status_code` | 模型服务响应状态；尚未取得响应为 `none` |

| `reason` | 含义 |
| --- | --- |
| `literal_number` / `forbidden_symbol` | 正文命中现有数字规则 / 不允许的测量符号或占位符规则 |
| `duplicate_reference` / `unknown_reference` | 引用重复 / 引用不存在或不可用；不记录引用内容 |
| `invalid_text_encoding` | 正文无法编码为 UTF-8 |
| `empty_content` / `invalid_content_type` | 模型正文为空 / 不是文本 |
| `invalid_model_json` / `schema_validation` | 模型正文不是合法 JSON / 不符合输出 Schema |
| `text_too_long` / `rendered_text_too_long` | 模型正文超长 / 追加参考测量后超长 |
| `duplicate_category` | 干预分类重复 |
| `invalid_response_json` / `invalid_response_structure` | 模型服务 HTTP 响应不是 JSON / 缺少所需响应结构 |
| `http_error` / `http_request_error` / `timeout` | HTTP 错误状态 / 网络请求错误 / HTTP 客户端超时 |
| `payload_error` / `analysis_error` | 请求体构造失败 / 其他分析链路错误 |

同一诊断信息也通过日志记录的 `failure_stage`、`failure_reason`、`failure_field`、`validation_type`、
`finish_reason`、`status_code` 属性提供；Schema 校验只记录首个错误的类型和位置。
`finish_reason=length` 可帮助识别服务报告的长度限制，但不据此新增拒绝、重试或降级规则。
工作流总超时仍由 `report_request_completed` 中的 `fallback_reason=timeout` 表示。

这些诊断日志不记录异常原文、完整身体成分数据、用户身份、密钥、未知字段内容、关键依据或分析正文。
模型输入、校验条件、输出篇幅不变；兜底策略除单条建议失败改为只丢弃该条外保持不变。

当前日志用于性能监控和后续数据表设计，暂不持久化报告原文和完整请求数据。
