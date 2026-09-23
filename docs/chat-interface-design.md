# Chat 接口设计文档

> 状态：设计中  
> 当前版本：v1.33  
> 最后更新：2026-09-23  
> 适用范围：Weight Agent Chat V1

## 1. 文档用途

本文档是 Chat 接口的设计基线，用于持续记录接口契约、工作流、数据边界、关键决策和实施进度。后续需求或实现发生变化时，应先更新对应章节和变更记录，再修改代码。

## 0. 配置管理约定

所有部署相关参数统一由项目根目录 `.env` 管理，并通过 `Settings` 注入应用：

- 服务基础配置：应用名、环境、监听地址、端口和 API 前缀。
- Chat 配置：意图识别阈值、短期记忆 TTL、最大轮次和默认时区/语言。
- Report 配置：报告模型名称、调用超时、温度、最大输出 token 数和 thinking 开关。
- 模型服务配置：模型服务地址、模型名称、调用超时、输出参数和 API 密钥。

本项目不训练、不部署本地模型，也不在服务内维护本地推理权重。意图识别、健康建议、报告生成等需要模型能力的模块统一通过外部模型 API 调用，并通过协议适配器隔离具体供应商。规则、词表、业务模板、评估集和安全护栏属于应用代码，不等同于模型训练。

规则关键词、意图枚举、路由契约和安全模板属于代码/领域策略，不作为环境变量拆散配置。数据库接入后，数据库连接信息单独增加 `DATABASE_*` 配置分组。

时间表达采用“规则优先、语义分类分层”的策略：明确周期由确定性代码计算日期；“最近一次/最近两次/最近 N 次”表示记录条数，不伪造日期范围，解析结果使用 `latest_count` 交给 Repository 查询最近 N 条记录；“最近的/前阵子”等无法确定范围的表达进入澄清或后续 LLM 时间意图解析。请求中的 `timezone` 优先级高于服务默认时区，最终写入 `NodeContext`、`MetricQuery` 和 `route_execution` 事件。

## 2. V1 目标

Chat 接口需要支持以下能力：

1. 查询用户的体重及相关健康指标数据。
2. 对指定周期的数据进行统计、趋势分析和周期对比。
3. 基于确定性分析结果和健康规则生成饮食、运动及生活方式建议。
4. 回答体重管理相关的通用健康咨询；必要时检索可信网络来源并提供引用。
5. 对非业务问题进行简短、友好的范围说明。
6. 通过 SSE 持续返回处理阶段、文本增量、引用和最终结果。

V1 不包含：

- 疾病诊断、处方、用药调整或紧急医疗决策。
- 允许模型自由生成或执行 SQL。
- 多个自治 Agent 之间的开放式协作。
- 长时间运行的离线报告生成。
- 本地训练、本地微调、本地模型推理或模型权重管理。

## 3. 核心设计决策

| 编号 | 决策 | 原因 | 状态 |
|---|---|---|---|
| ADR-001 | 使用单个 LangGraph 状态图编排 Chat | 需要分支、工具调用、状态保存和失败恢复，但 V1 不需要多 Agent | 已确定；当前使用可替换工作流占位 |
| ADR-002 | 指标查询与统计由确定性服务完成 | 避免 LLM 算错数据或产生不存在的指标 | 已确定 |
| ADR-003 | 建议采用“规则事实 + LLM 表达” | 保证建议有依据，同时保留自然语言体验 | 已确定 |
| ADR-004 | 流式协议采用结构化 SSE 事件 | 前端可以分别展示进度、正文、引用和错误 | 已确定 |
| ADR-005 | 前端使用 `fetch` 消费 POST SSE | 浏览器原生 `EventSource` 不支持 POST 请求体和自定义请求头 | 已确定 |
| ADR-006 | 一期不联网检索，使用模型能力和审核后的业务规则 | 降低 C 端响应延迟、外部依赖和来源治理成本；保留未来扩展点 | 已确定 |
| ADR-007 | V1 不保存 LangGraph checkpoint | 首先保持请求内工作流简单；多轮持久化单独设计 | 暂定 |
| ADR-008 | 通过 Repository 契约隔离未知数据库表结构 | Chat 工作流依赖领域模型而不是具体表和 SQL，后续只替换适配器 | 已确定 |
| ADR-009 | 支持带 TTL 的短期会话记忆，不建立长期用户记忆 | 满足多轮对话，同时降低健康数据长期留存和隐私治理成本 | 已确定 |

## 4. 总体架构

```mermaid
flowchart TD
    Client[客户端] -->|POST /api/v1/chat/stream| API[FastAPI Chat Route]
    API --> Guard[请求校验与安全检查]
    Guard --> Graph[LangGraph Chat Workflow]

    Graph --> Intent[意图识别与参数提取]
    Intent -->|query / analysis| MetricTool[指标查询工具]
    MetricTool --> Analytics[统计与周期对比服务]
    Analytics --> Rules[健康建议规则引擎]
    Rules --> Compose[LLM 答复生成]

    Intent -->|health_consultation| Search[可信健康信息检索]
    Search --> Compose

    Intent -->|out_of_scope| ScopeReply[范围说明]
    Compose --> Validate[结果校验]
    ScopeReply --> Stream[SSE 事件流]
    Validate --> Stream
    Stream --> Client
```

职责边界：

- API 层：鉴权、请求校验、断开检测、SSE 响应头和错误映射。
- LangGraph：控制节点执行顺序和条件分支，不承载业务计算。
- Tool/Service：执行受控查询、统计、比较和规则判断；联网检索属于二期可选扩展。
- LLM：结构化理解用户意图，并根据已验证事实组织自然语言。
- Repository：隔离数据库实现，不向 LLM 暴露数据库连接或任意 SQL 能力。

## 5. 接口契约

### 5.1 端点

```http
POST /api/v1/chat/stream
Content-Type: application/json
Accept: text/event-stream
Authorization: Bearer <token>
```

V1 只提供流式端点。是否补充非流式 `/api/v1/chat`，在前端接入后根据实际需要决定。

### 5.2 请求模型

```json
{
  "conversation_id": "可选；已有会话 ID",
  "message": "帮我分析最近一个月的数据情况，并给出饮食建议",
  "timezone": "Asia/Shanghai",
  "client_context": {
    "locale": "zh-CN"
  }
}
```

字段约束：

| 字段 | 类型 | 必填 | 约束 |
|---|---|---:|---|
| `conversation_id` | string | 否 | UUID；缺省时服务端创建 |
| `message` | string | 是 | 去除首尾空格后 1-4000 字符 |
| `timezone` | string | 否 | IANA 时区；默认取用户资料，仍缺失则使用系统默认时区 |
| `client_context.locale` | string | 否 | 客户端偏好语言/区域，例如 `zh-CN`、`en-US`；缺省使用服务默认值 |

`client_context.locale` 表示客户端配置或用户偏好，不一定等同于本轮实际输入语言。服务端在语义解析阶段维护本轮的 `detected_language` 和 `response_language`：默认按用户本轮实际使用的主要语言回复；用户在消息中明确要求另一种语言时遵循该要求；语言检测置信度不足时回退到客户端 `locale`，最后回退到服务默认语言。客户端偏好不会覆盖用户本轮明确表达的语言要求。

语言代码采用 BCP 47 风格标签，例如 `zh-CN`、`en-US`、`ja-JP`。内部语言检测也可先使用基础语言代码（如 `zh`、`en`、`ja`），需要决定地区格式时再结合客户端 `locale`。

`user_id` 不允许由请求体传入，必须来自认证上下文，防止查询其他用户数据。

当前代码阶段使用 `X-User-Id` 请求头作为开发期身份注入占位，便于接口契约测试；它不是生产认证方案。接入正式认证后，应由认证中间件或依赖注入提供 `current_user`，并移除客户端可控的该请求头。

### 5.3 HTTP 响应头

```http
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

在 SSE 建连之前发生的认证或请求格式错误，使用普通 HTTP 状态码和 JSON 错误体。SSE 建连之后发生的业务或工具错误，通过 `error` 事件返回。

## 6. SSE 事件协议

所有事件的 `data` 都是单行 JSON。调试模式事件包含 `request_id`、`sequence` 和
`timestamp`，便于排序和排障；默认公共模式只返回客户端展示所需的最小字段。

### 6.1 事件类型

| 事件 | 用途 | 是否可重复 |
|---|---|---:|
| `start` | 确认会话和请求已创建 | 否 |
| `progress` | 展示当前处理阶段 | 是 |
| `data` | 返回意图识别结果、查询范围和核心统计结果；仅调试模式发送 | 是 |
| `delta` | 按模型 token 或最小文本片段返回自然语言增量 | 是 |
| `citation` | 返回资料来源；一期不发送，二期联网后启用 | 是 |
| `warning` | 数据不足或健康风险提示 | 是 |
| `error` | 流建立后的错误 | 否 |
| `done` | 请求正常结束及最终元数据 | 否 |

### 6.2 示例

默认公共模式：

```text
event: start
data: {"conversation_id":"conv_123"}

event: delta
data: {"content":"最近"}

event: delta
data: {"content":" 30 天的数据如下……"}

event: done
data: {"status":"completed","conversation_id":"conv_123"}
```

调试模式（`WEIGHT_AGENT_CHAT_EXPOSE_DEBUG_EVENTS=true`）：

```text
event: start
data: {"request_id":"req_123","sequence":1,"timestamp":"2026-09-20T12:00:00Z","conversation_id":"conv_123"}

event: progress
data: {"request_id":"req_123","sequence":2,"timestamp":"2026-09-20T12:00:00Z","stage":"querying","message":"正在查询最近一个月的数据"}

event: data
data: {"request_id":"req_123","sequence":3,"timestamp":"2026-09-20T12:00:01Z","type":"intent_result","intent_result":{"domain":"health_data","intent":"metric_analysis","output_needs":["data","comparison"],"confidence":0.84}}

event: delta
data: {"request_id":"req_123","sequence":4,"timestamp":"2026-09-20T12:00:01Z","content":"最近 30 天，"}

event: done
data: {"request_id":"req_123","sequence":5,"timestamp":"2026-09-20T12:00:03Z","status":"completed"}
```

### 6.3 流行为约束

- `start` 必须是第一个事件。
- 正常结束必须且只能发送一次 `done`。
- `error` 是终止事件，发送后关闭连接，不再发送 `done`。工作流主体异常统一在
  事件边界转换为 `error`（错误码见第 13 节），客户端不会遇到连接裸断。
- 服务端每 15 秒发送 SSE 注释心跳，防止代理关闭空闲连接。
- 客户端断开后，服务端应取消当前图执行和下游 HTTP 请求。
- 默认客户端模式只发送 `start`、多个 `delta`、一次 `done`，异常时以 `error`
  结束；`error` 在公共模式下只保留 `code`、`message` 和 `retryable` 三个字段。
  规则版按短句或约 12 个字符的适中片段逐步返回正文，避免逐字符刷新造成界面抖动。
- `delta` 只包含面向用户的正文，不混入调试信息或思维过程。
- `data(type=intent_result)`、`data(type=supervisor_plan)` 和
  `data(type=route_execution)` 只在 `WEIGHT_AGENT_CHAT_EXPOSE_DEBUG_EVENTS=true`
  时发送，用于开发排查，不作为默认客户端协议。
- 接入支持原生流式输出的 LLM 后，`delta` 应直接转发模型产生的 chunk；当前规则版
  使用短句/小段文本模拟同一接口行为，不要求一个 SSE 事件只包含一个字符。

## 7. 意图识别设计

### 7.1 设计原则

意图识别参考常见开源对话系统的做法：分类结果使用固定枚举，槽位单独抽取，低置信度进入澄清分支，模型失败时由规则和模板兜底。V1 不采用让 LLM 自由规划任务的方式。

识别结果不是最终业务结论，只是后续图节点的路由输入。数据库查询、时间归一化和指标计算仍然由确定性代码完成。

### 7.2 分层结果模型

```python
class ChatDomain(str, Enum):
    GENERAL = "general"
    HEALTH_DATA = "health_data"
    HEALTH_KNOWLEDGE = "health_knowledge"
    OUT_OF_SCOPE = "out_of_scope"


class ChatIntent(str, Enum):
    GREETING = "greeting"
    METRIC_QUERY = "metric_query"
    METRIC_ANALYSIS = "metric_analysis"
    DATA_BASED_ADVICE = "data_based_advice"
    DOMAIN_ADVICE = "domain_advice"
    OUT_OF_SCOPE = "out_of_scope"


class OutputNeed(str, Enum):
    DATA = "data"
    COMPARISON = "comparison"
    ADVICE = "advice"


class RiskLevel(str, Enum):
    NONE = "none"
    MEDICAL_REVIEW = "medical_review"
    URGENT = "urgent"


class IntentResult(BaseModel):
    domain: ChatDomain
    intent: ChatIntent
    output_needs: list[OutputNeed]
    entities: QueryEntities
    confidence: float = Field(ge=0, le=1)
    risk_level: RiskLevel = RiskLevel.NONE
    needs_clarification: bool = False
    clarification_question: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
```

`reason_codes` 只记录可审计的分类依据，例如 `contains_metric_name`、`contains_compare_phrase`、`contains_symptom_term`，不记录模型思维过程。

### 7.3 V1 意图枚举

| 意图 | 触发特征 | 后续路由 |
|---|---|---|
| `greeting` | 纯问候、能力介绍或使用引导，不包含业务查询/分析/建议需求 | `greeting_reply` |
| `metric_query` | 查询当前值、历史值或某段时间的指标 | `normalize_query -> query_metrics -> compose_answer` |
| `metric_analysis` | 包含分析、趋势、变化、对比、原因或建议 | `normalize_query -> query_metrics -> analyze_metrics` |
| `data_based_advice` | 明确要求根据自己的数据给出饮食、运动或生活方式建议 | `normalize_query -> query_metrics -> analyze_metrics -> evaluate_rules` |
| `domain_advice` | 不查询个人数据，直接询问体重管理业务范围内的饮食、运动、睡眠建议 | `business_advice -> compose_answer` |
| `out_of_scope` | 与健康数据和健康管理无关 | `scope_reply` |

以下不是普通意图，而是跨意图路由状态：

- `needs_clarification`：关键指标、时间或用户目标缺失，不能可靠执行。
- `risk_level=medical_review`：需要谨慎说明并建议专业咨询。
- `risk_level=urgent`：进入安全模板，不继续普通回答。

### 7.4.1 业务建议的两种入口

饮食建议、运动建议和生活方式建议属于业务范围内能力，不应默认归为泛健康咨询：

1. **数据型建议**：用户明确要求结合自己的体重、BMI、体脂或历史趋势给建议。必须先查询并分析用户数据，再引用分析事实和规则生成建议。
2. **领域型建议**：用户直接询问减脂饮食、早餐搭配、热量控制、运动安排等业务知识，不要求读取个人数据。使用模型能力和已审核的业务知识、规则直接回答，一期不联网。

两者都必须限制在体重管理和生活方式指导范围内。涉及疾病诊断、处方药、药量调整、急症或个体化医疗处置时，转入医疗风险分支。

### 7.4 多需求表达

一句话可以包含多个输出需求，但只保留一个主意图：

| 用户输入 | 主意图 | 输出需求 |
|---|---|---|
| “你好” | `greeting` | 无 |
| “我今天多重？” | `metric_query` | `data` |
| “你好，帮我查一下最近一个月的测量数据” | `metric_query` | `data`；缺少指标时澄清 |
| “分析最近一个月并给饮食建议” | `metric_analysis` | `data, comparison, advice` |
| “和上个月比一下体重，再告诉我怎么吃” | `metric_analysis` | `data, comparison, advice` |
| “根据我的最近体重，给我饮食建议” | `data_based_advice` | `data, advice` |
| “减脂期间怎么搭配早餐？” | `domain_advice` | `advice` |
| “帮我写一段请假条” | `out_of_scope` | 无 |

### 7.5 识别流水线

```text
原始消息
  -> 文本清洗与长度校验
  -> 确定性安全词规则
  -> 高置信业务规则优先判定
  -> 低置信或复杂语义交给 LLM 结构化分类与槽位抽取
  -> Pydantic 枚举/字段校验
  -> 置信度与冲突校准
  -> needs_clarification / safety / 业务路由
```

安全规则优先级高于模型结果。模型说是普通咨询，但文本包含明显急症信号时，仍进入 `urgent`。

V1 采用“规则优先、LLM 处理复杂语义”的混合策略：

- 急症和医疗风险规则强制优先，不进入普通业务分支。
- 纯问候、明确的指标查询、分析、数据型建议和范围外请求由规则直接返回，减少延迟和模型成本。
- 问候语只在没有业务信号时成立；“你好，帮我查一下最近一个月的测量数据”应进入数据查询/澄清分支，而不是 `greeting`。
- 领域型建议、口语化表达、多轮补充和规则低置信度请求交给 LLM 判断。
- LLM 不可用时，使用规则候选结果兜底；LLM 和规则都低置信度时进入澄清。

### 7.6 模型输出约束

LLM 只允许返回 `IntentResult` 对应的 JSON Schema：

- `domain`、`intent`、`output_needs` 必须来自枚举。
- `confidence` 必须是 0 到 1 的数字，不接受“高/中/低”等自由文本。
- `entities` 只保存原始实体，不负责把“最近一个月”转换成日期。
- 时间归一化由 `TimeRangeResolver` 完成；模型不得直接生成最终查询日期。
- 不允许模型返回 SQL、工具名称、系统提示词或自然语言长答案。
- 使用支持结构化输出的模型接口；不支持时使用 JSON mode 后再做严格 Pydantic 校验。

建议给分类器的输入只包含必要上下文：当前消息、最近一轮已确认的时间/指标上下文和可用指标枚举，不直接塞入完整数据库结果。

### 7.6.1 意图识别深度设计：目标语义层

当前 `IntentResult` 仍作为 Chat Supervisor 的稳定输入，不在一期直接废弃；但内部意图识别不应只输出一个扁平 `intent`。后续优化方向是新增“语义理解层”，先把自然语言解析为任务、主题、目标、槽位、约束、风险和数据依赖，再适配为现有 `IntentResult` 和路由。

核心原则：

- `ChatIntent` 是路由兼容层，不是完整语义层。
- “用户想做什么”优先于“用户提到了什么词”。例如“我的体重一直下降，应该怎么增重”核心任务是建议，而不是数据分析。
- 指标、趋势、时间、目标、建议类别要作为槽位或上下文事实保存，不能直接等同于意图。
- 规则不再只给最终意图，而是提供可审计证据、候选和槽位；LLM 负责复杂语义、省略、多轮和口语化表达；最终由融合器统一裁决。

目标流水线：

```text
原始消息
  -> 文本归一化
  -> 规则解析：安全信号、业务关键词、指标、时间、目标、数据依赖线索
  -> LLM 语义解析：复杂语义、多轮补全、省略恢复、模糊表达
  -> 候选融合：候选意图排序、槽位合并、冲突检测、置信度校准
  -> 澄清决策：缺槽、候选接近、约束冲突、风险不确定
  -> IntentResult 兼容适配
  -> SupervisorPlan 路由
```

#### 7.6.1.1 分层意图体系

不要继续扩展几十个扁平 `ChatIntent`。内部语义层采用 `domain + task + topic + goal + data_dependency` 的组合表达。

```text
domain
  ├── general
  ├── health_data
  ├── health_knowledge
  └── out_of_scope

task
  ├── greeting
  ├── query
  ├── analysis
  ├── advice
  ├── compare
  ├── explain
  ├── clarify
  └── out_of_scope

topic
  ├── weight
  ├── bmi
  ├── body_fat
  ├── waist
  ├── diet
  ├── exercise
  ├── sleep
  ├── lifestyle
  └── general_health

goal
  ├── weight_loss
  ├── weight_gain
  ├── fat_loss
  ├── muscle_gain
  ├── maintain_weight
  └── improve_lifestyle
```

`data_dependency` 用于判断是否必须查询用户数据：

| 值 | 含义 | 示例 | 目标路由 |
|---|---|---|---|
| `none` | 不需要个人数据也能回答 | “减肥期间吃什么” | `domain_advice` |
| `optional` | 用户提到个人情况，但没有数据也可以给通用建议 | “我的体重一直下降，应该怎么增重” | `domain_advice` |
| `required` | 必须读取个人指标数据才能完成 | “根据最近一个月体重变化给我饮食建议” | `data_based_advice` |

典型映射：

| 用户表达 | 内部语义 | 兼容意图 | 路由 |
|---|---|---|---|
| “帮我查体重” | `task=query, topic=weight, data_dependency=required` | `metric_query` | `data_analysis` |
| “帮我分析最近一个月体重变化” | `task=analysis, topic=weight, data_dependency=required` | `metric_analysis` | `data_analysis` |
| “根据最近一个月体重变化给我饮食建议” | `task=advice, topic=[weight,diet], data_dependency=required` | `data_based_advice` | `data_based_advice` |
| “减肥期间吃什么” | `task=advice, topic=diet, goal=weight_loss, data_dependency=none` | `domain_advice` | `domain_advice` |
| “我的体重一直下降，应该怎么增重” | `task=advice, topic=weight, goal=weight_gain, data_dependency=optional` | `domain_advice` | `domain_advice` |

#### 7.6.1.2 语义模型草案

后续可以在 `chat/models.py` 中新增内部模型，先不直接暴露给 API：

```python
class TaskType(StrEnum):
    GREETING = "greeting"
    QUERY = "query"
    ANALYSIS = "analysis"
    ADVICE = "advice"
    COMPARE = "compare"
    EXPLAIN = "explain"
    CLARIFY = "clarify"
    OUT_OF_SCOPE = "out_of_scope"


class TopicType(StrEnum):
    WEIGHT = "weight"
    BMI = "bmi"
    BODY_FAT = "body_fat"
    WAIST = "waist"
    DIET = "diet"
    EXERCISE = "exercise"
    SLEEP = "sleep"
    LIFESTYLE = "lifestyle"
    GENERAL_HEALTH = "general_health"


class DataDependency(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class SlotSource(StrEnum):
    USER = "user"
    RULE = "rule"
    LLM = "llm"
    CONTEXT = "context"
    DEFAULT = "default"


class SlotValue(BaseModel):
    name: str
    value: str | list[str] | None
    canonical_value: str | list[str] | None = None
    source: SlotSource
    confidence: float
    confirmed: bool = False
    required: bool = False
    conflict: bool = False


class IntentCandidate(BaseModel):
    task: TaskType
    intent: ChatIntent
    topics: list[TopicType]
    data_dependency: DataDependency
    score: float
    evidence: list[str]
    missing_slots: list[str]


class SemanticParse(BaseModel):
    domain: ChatDomain
    task: TaskType
    topics: list[TopicType]
    goals: list[str]
    data_dependency: DataDependency
    detected_language: str
    response_language: str
    slots: list[SlotValue]
    candidates: list[IntentCandidate]
    selected_candidate_index: int | None = None
    confidence: float
    risk_level: RiskLevel
    needs_clarification: bool = False
    clarification_question: str | None = None
    reason_codes: list[str]
```

`SemanticParse` 是内部中间结果，最后再通过适配器转换为当前 `IntentResult`。这样可以保持现有 `ChatSupervisor`、`RouteExecutor` 和接口测试稳定。

#### 7.6.1.3 槽位体系

一期重点槽位：

| 槽位 | 用途 | 是否必填取决于 |
|---|---|---|
| `metric` | 体重、BMI、体脂率、腰围等 | 查询/分析/数据型建议 |
| `time_expression` | 最近一个月、最近三次、今天等 | 数据查询类任务 |
| `comparison_mode` | 环比、目标对比、前后对比 | 对比/分析任务 |
| `goal` | 减重、增重、减脂、维持等 | 建议类任务 |
| `advice_category` | 饮食、运动、睡眠、生活方式 | 建议类任务 |
| `risk_signal` | 急症、医疗风险、诊疗诉求 | 所有任务 |
| `data_dependency` | 是否需要个人数据 | 路由决策 |
| `timezone` | 时间归一化 | 时间表达解析 |
| `client_locale` | 客户端声明的偏好语言/区域 | 语言检测低置信时的回退 |
| `detected_language` | 本轮消息的主要语言 | 决定本轮回复语言 |
| `response_language` | 经优先级规则确定的回复语言 | 意图澄清和最终答复 |

语言决策优先级：

```text
用户本轮明确指定的回复语言
  > 本轮消息检测出的主要语言
  > client_context.locale
  > 服务默认语言
```

用户本轮语言与客户端 locale 不同，且没有明确指定回复语言时，默认跟随本轮消息语言。例如客户端 locale 为 `zh-CN`，但用户本轮用英文提问，则用英文回答。用户明确要求“请用中文回答”时，则使用中文，即使输入主体是英文。

语言检测作为轻量语义元数据处理：优先使用本地确定性检测或现有模型意图解析的附带字段，不单独增加一次模型 API 调用。混合语言消息按主要表达语言决定；主要语言无法可靠判断时回退到客户端 locale。后续如果实际 bad case 表明本地检测不足，可以在原有模型 API 请求中要求结构化返回语言字段，而不是新增独立翻译调用。

答复模型应直接按 `response_language` 生成文本，包括澄清问题、范围说明、安全提示和健康建议。正常路径不增加“先生成一种语言、再调用翻译 API”的步骤，减少额外延迟、费用和翻译偏差。若未来确需翻译，应作为明确的可选能力单独评估。

槽位合并规则：

- 当前消息显式槽位优先级最高。
- 用户纠正表达覆盖历史槽位，例如“不，我是想增重”覆盖上一轮 `weight_loss`。
- 历史上下文只在当前消息省略时补齐，不得覆盖当前消息。
- 规则和 LLM 对同一槽位冲突时，保留冲突标记并交给澄清决策。
- 默认槽位必须标记 `source=default`，并在回复中说明，例如“如果你没有指定时间，我先按最近 30 天处理”。

#### 7.6.1.4 上下文与多轮理解

当前项目已经有 `recent_turns`、`confirmed_entities`、`pending_entities` 和 `last_intent`。后续 DST 应补充以下状态：

```python
class DialogueState(BaseModel):
    active_task: TaskType | None = None
    active_topics: list[TopicType] = []
    active_goal: str | None = None
    confirmed_slots: list[SlotValue] = []
    pending_slots: list[SlotValue] = []
    last_candidates: list[IntentCandidate] = []
    last_route: SupervisorRoute | None = None
    clarification_attempts: int = 0
    last_user_correction: bool = False
```

多轮规则：

- 用户新消息优先于历史状态。
- 用户短回答如果命中上一轮 `pending_slots`，应视为澄清回答，而不是新任务。
- 用户出现“不是、改成、我说的是、不对”等纠正信号时，清理冲突槽位并重新解析。
- 话题继承只在用户省略主语/对象时发生，例如“最近三次”继承上一轮的指标和任务。
- 连续澄清达到 2 次后，应给出可执行默认方案或范围说明，避免反复追问。

必须覆盖的多轮样例：

```text
用户：帮我分析体重
系统：你想分析最近 30 天还是最近几次？
用户：最近三次
=> 继承 topic=weight，补齐 time_expression=最近三次，路由 data_analysis
```

```text
用户：我想减肥
用户：不，我是想增重
=> goal 从 weight_loss 修正为 weight_gain
```

```text
用户：帮我查一下最近一个月体重
用户：再给点饮食建议
=> 继承 topic=weight 和时间范围，第二轮可走 data_based_advice 或 domain_advice，取决于是否明确要结合数据
```

#### 7.6.1.5 歧义、多候选与多意图

一期支持有限多意图，不做任意任务编排。

明确支持：

```text
查询/分析 + 建议
```

例如：

```text
帮我分析最近一个月体重变化，并告诉我怎么调整饮食
```

内部可以分解为：

```text
1. task=analysis, topic=weight, data_dependency=required
2. task=advice, topic=diet, data_dependency=required
```

执行顺序固定为：

```text
data_analysis -> business_advice
```

候选排序优先级：

```text
安全风险 > 明确行动目标 > 数据依赖 > 查询/分析 > 泛化建议 > 问候
```

因此：

```text
“你好，帮我查一下最近一个月体重”
```

不能判为问候；而：

```text
“我的体重一直下降，应该怎么增重”
```

应优先识别为建议任务，`下降` 只是背景事实，不是分析任务的充分条件。

#### 7.6.1.6 澄清回路

澄清触发不应只依赖单个 `confidence`，而应综合：

```text
missing_required_slot
or candidate_margin_too_small
or slot_conflict
or risk_uncertain
or scope_uncertain
```

澄清类型：

| 类型 | 用途 | 示例 |
|---|---|---|
| `slot_filling` | 补关键槽位 | “你想分析体重、BMI、体脂率还是腰围？” |
| `selection` | 多候选选择 | “你是想查看数据，还是想分析变化趋势？” |
| `confirmation` | 确认高风险或冲突理解 | “你是想了解一般建议，而不是医疗诊断，对吗？” |
| `repair` | 处理纠正和恢复 | “好的，我按增重目标重新给你建议。” |
| `scope` | 超范围说明 | “这个问题超出体重管理范围，我可以帮你看体重、饮食和运动相关问题。” |

澄清策略：

- 一次只问一个最关键问题。
- 先问影响路由的问题，再问展示细节。
- 简单领域建议尽量不澄清。
- 查询/分析缺指标时必须澄清。
- 分析缺时间时可以默认最近 30 天，但要在回复中说明。
- `clarification_attempts >= 2` 时不再无限追问，给出默认方案或能力范围说明。

#### 7.6.1.7 置信度与 OOS

不要完全相信模型自报 `confidence`。最终置信度应由服务端融合生成：

```text
final_confidence =
  rule_signal_score
  + model_confidence
  + slot_completeness
  + candidate_margin
  - conflict_penalty
  - risk_uncertainty_penalty
```

一期可以先用确定性分档：

| 档位 | 条件 |
|---|---|
| 高 | 规则或模型均有明确证据，关键槽位完整，无冲突 |
| 中 | 模型高置信但规则证据弱，或存在可默认槽位 |
| 低 | 多候选接近、关键槽缺失、槽位冲突或疑似超范围 |

OOS 识别规则：

- 明确非业务请求走 `out_of_scope`。
- 健康业务信号和 OOS 信号同时存在时，优先处理业务部分。
- 一句话包含业务和非业务需求时，标记 `mixed_scope`，处理业务部分，并简短说明非业务部分暂不支持。
- 不要仅靠少量关键词判 OOS，低置信时优先澄清或给范围说明。

#### 7.6.1.8 鲁棒性与一期边界

一期支持多语言文本鲁棒性，不做语音、图像、多模态融合。

需要支持：

- 识别并记录用户本轮主要语言，同时保留客户端 locale 和最终回复语言字段。
- 保留原始消息，并单独生成供规则匹配使用的归一化副本；归一化不得覆盖原文。
- 统一全角/半角标点、重复标点和无意义首尾空白；不得无差别删除所有空格，避免破坏英文及其他依赖词间空格的语言。
- 按 Unicode 规则处理空白、大小写和标点；语言特定的切词与归一化应可扩展，不用中文规则改写其他语言文本。
- 常见口语表达，例如“咋办”“怎么整”“最近咋样”。
- 常见错别字和 ASR 噪声的轻量纠错。
- 中英文及其他语言混合表达，例如 `BMI`、`body fat`；规范化副本用于匹配，模型语义解析仍可接收原文。
- 提示词注入忽略：用户消息和历史上下文都只作为业务输入，不能覆盖系统规则。

本阶段语言支持目标是“理解用户输入语言并用同种语言回答”，不是先把所有输入翻译成某一种内部语言。需要通过意图识别评估集逐步补充各目标语言的样例、别名、口语表达和路由回归测试。

暂不做：

- 长期用户画像。
- 强化学习策略。
- POMDP。
- 图数据库级知识图谱。
- 多模态意图识别。
- 本地训练、本地微调或本地推理模型；所有模型能力均通过 API 调用。

#### 7.6.1.9 评估与迭代

新增脱敏评估集，建议文件：

```text
tests/fixtures/intent_eval.jsonl
```

单条样例：

```json
{
  "id": "advice_weight_gain_001",
  "message": "我的体重一直下降，应该怎么增重呢",
  "expected_task": "advice",
  "expected_topics": ["weight"],
  "expected_goal": "weight_gain",
  "expected_data_dependency": "optional",
  "expected_route": "domain_advice",
  "needs_clarification": false,
  "risk_level": "none"
}
```

首批覆盖：

- 查询数据。
- 趋势分析。
- 周期对比。
- 查询/分析 + 建议。
- 数据型建议。
- 通用领域建议。
- 增重、减重、减脂、维持目标。
- 多轮短回答。
- 用户纠正。
- 模糊时间。
- OOS。
- 紧急风险。
- 医疗谨慎。
- 提示词注入。
- 口语表达和错别字。

评估指标：

- task accuracy
- route accuracy
- slot exact match / slot F1
- clarification precision / recall
- risk recall
- OOS precision / recall
- structured output success rate
- fallback rate
- latency

#### 7.6.1.10 分阶段实施计划

阶段一：文档和模型

- 固化本章节设计。
- 新增内部枚举和 Pydantic 模型。
- 保持 `IntentResult` 对外兼容。

阶段二：语言感知与文本归一化

- 新增语言决策字段和优先级实现：`client_locale`、`detected_language`、`response_language`。
- 新增保留原文的文本归一化器；归一化文本只作为规则匹配副本。
- 为中文、英文及后续目标语言分别建立归一化和检测测试。

阶段三：语义解析器

- 拆出规则解析器，输出证据、候选和槽位。
- 复杂请求调用模型 API 时要求按 `response_language` 生成结构化语义结果；不增加单独翻译调用。

阶段四：融合与澄清

- 新增候选融合器。
- 新增槽位冲突检测。
- 新增澄清决策器。
- 将结果适配回 `IntentResult`。

阶段五：多轮 DST

- 扩展 `ConversationMemory` 保存轻量 `DialogueState`。
- 完善澄清回答解析、话题继承、用户纠正。
- 部署时再将内存版本替换为 Redis 版本。

阶段六：评估闭环

- 新增 `intent_eval.jsonl`。
- 新增批量评估脚本。
- 将线上 bad case 脱敏后沉淀为回归用例。
- 增加多语言 task/route/slot 准确率、语言跟随准确率和按语言分组的延迟评估。

### 7.7 置信度与澄清策略

V1 先使用保守阈值，经过标注集评估后再调整：

| 条件 | 处理 |
|---|---|
| `confidence >= 0.80` 且实体完整 | 直接进入对应业务路由 |
| `0.55 <= confidence < 0.80` | 先检查规则冲突；无冲突时可执行简单查询，否则澄清 |
| `confidence < 0.55` | 进入澄清或范围说明，不查询数据库 |
| 多个候选意图分数接近 | 询问一个最小澄清问题，不连续追问 |
| 意图明确但缺少时间范围 | 查询类可使用默认最近一次；分析类必须确认或使用明确默认并在事件中说明 |

澄清问题示例：

- “你想查看体重、BMI 还是体脂率？”
- “你想看最近 30 天，还是指定一个起止日期？”
- “你希望与上一周期比较，还是只查看当前数据？”

澄清响应也通过 SSE 返回：`progress(stage=clarification)`，随后发送 `done(status=needs_clarification)`，不调用指标 Repository。

### 7.8 规则兜底与可测试性

意图识别必须允许注入 `IntentClassifier`，以便离线测试：

```python
class IntentClassifier(Protocol):
    async def classify(self, message: str, context: ClassifierContext) -> IntentResult: ...
```

生产实现可以是 `LlmIntentClassifier`，测试实现使用固定映射或规则分类器。规则兜底只负责明显模式：

- 指标名称 + “多少/当前/记录” -> `metric_query`。
- “你好/在吗/你能做什么” 且不包含业务请求 -> `greeting`。
- “分析/趋势/变化/对比/建议” + 指标或时间 -> `metric_analysis`。
- “根据我的数据/体重趋势给建议” -> `data_based_advice`。
- “怎么吃/怎么运动/减脂/早餐/睡眠” -> `domain_advice`。
- 明显非健康主题 -> `out_of_scope`。

规则不能替代完整语义识别，也不能用关键词命中直接判定医疗结论。

### 7.9 评估集

在接入真实模型前，先建立脱敏的意图评估集，每条样本包含：

- 用户原话。
- 期望 `domain`、`intent`、`output_needs`。
- 必填/可选实体。
- 风险级别。
- 是否应该澄清。

首批至少覆盖：单指标查询、范围查询、周期对比、查询+建议、多轮补充、模糊时间、无关问题、症状问题、提示注入和口语/错别字。评估指标包括主意图准确率、澄清准确率、风险召回率和结构化输出成功率。

## 8. LangGraph 状态设计

状态只存储图执行所需的结构化数据，不保存数据库连接、完整网页内容或模型内部推理。

```python
class ChatState(TypedDict, total=False):
    request_id: str
    conversation_id: str
    user_id: str
    message: str
    timezone: str
    locale: str

    intent_result: IntentResult
    intent: ChatIntent
    output_needs: list[OutputNeed]
    entities: QueryEntities
    route: str

    metric_query: MetricQuery
    metric_result: MetricResult
    analysis_result: AnalysisResult
    rule_findings: list[RuleFinding]
    sources: list[HealthSource]  # 二期联网能力使用；一期为空

    answer: str
    warnings: list[ChatWarning]
    error: ChatError | None
```

关键结构：

- `QueryEntities`：指标、时间表达、对比方式、建议类别。
- `MetricQuery`：归一化后的查询参数，只能由服务端验证后生成。
- `MetricResult`：查询结果及数据完整性信息。
- `AnalysisResult`：统计值、变化量、趋势和异常点。
- `RuleFinding`：规则编号、事实、风险级别和建议素材。
- `HealthSource`：标题、URL、机构、发布日期和访问时间。

## 8.1 主 Agent：Chat Supervisor

主 Agent 是 Chat 的统一编排者，不是一个自由回答所有问题的“大模型节点”。它负责把用户请求转换为受控的业务执行计划，并把专业工作交给对应 Agent 或确定性服务。

### 主 Agent 的职责

1. 接收经过 API 校验和认证的用户请求。
2. 读取必要的会话上下文，不直接读取数据库原始数据。
3. 调用意图识别器，得到结构化的 `IntentResult`。
4. 判断请求是否属于体重管理业务范围。
5. 判断是否存在医疗风险、紧急信号或需要人工/专业介入的内容。
6. 判断是否需要澄清指标、时间范围、对比方式或用户目标。
7. 根据意图选择专业 Agent：数据分析 Agent、业务建议 Agent、安全回复或范围回复。
8. 在数据型建议场景中，安排“数据分析 Agent -> 业务建议 Agent”的固定顺序。
9. 只在 Agent 返回结构化结果后组织最终回答流程。
10. 通过 SSE 发布阶段状态，不向用户暴露内部推理过程。

### 主 Agent 不负责的事情

- 不直接生成 SQL，也不选择表名或字段名。
- 不直接计算变化率、趋势、BMI 或其他健康指标。
- 不自行编造健康规则、目标范围或用户数据。
- 不把数据分析 Agent 和业务建议 Agent 的职责混在一起。
- 不绕过安全节点回答高风险医疗问题。
- 不允许用户输入覆盖认证得到的 `user_id`。
- 不在一期调用联网搜索。

### 主 Agent 输入

```python
class SupervisorInput(BaseModel):
    request_id: str
    conversation_id: str
    user_id: str
    message: str
    timezone: str
    locale: str
    conversation_summary: str | None = None
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    confirmed_entities: QueryEntities | None = None
    last_analysis: AnalysisResult | None = None
```

`user_id` 和会话权限来自服务端上下文；`message` 是经过长度和空白校验的文本；`conversation_summary` 只能是经过裁剪的历史摘要，不直接传入完整敏感历史。

### 主 Agent 输出

```python
class SupervisorPlan(BaseModel):
    route: Literal[
        "greeting",
        "data_analysis",
        "data_based_advice",
        "domain_advice",
        "safety",
        "out_of_scope",
        "clarification",
    ]
    intent_result: IntentResult
    required_agents: list[Literal["data_analysis", "business_advice"]]
    requires_metric_query: bool
    clarification_question: str | None = None
    safety_message: str | None = None
```

主 Agent 的输出必须是可校验的计划，不是最终答案。计划生成后由确定性路由器再次检查：例如 `domain_advice` 不得出现 `requires_metric_query=true`，`safety` 不得继续调用专业建议 Agent。

当前实现采用确定性 `ChatSupervisor` 生成计划：输入是已经通过 Pydantic 校验的 `IntentResult`，输出是 `SupervisorPlan`。一期不让 LLM 自由生成计划，避免工具调用、数据库访问和安全路由不可控。

节点层使用统一的异步协议：节点接收 `SupervisorPlan` 和服务端 `NodeContext`，返回结构化 `NodeResult`。节点不能自行改变路由，也不能绕过计划直接访问未注入的外部资源。

`RouteExecutor` 根据 `SupervisorPlan.route` 调用对应节点。对于 `data_based_advice`，执行顺序固定为 `data_analysis -> business_advice`，不会由模型动态改变。

`DataAnalysisNode` 通过构造函数预留 `MetricRepository` 依赖注入口。数据库表结构确定后，只需注入新的数据库适配器（例如 `SqlMetricRepository`），不需要改动 Supervisor 路由、SSE 协议或业务建议节点。

### 主 Agent 路由表

| 条件 | 路由 | 执行顺序 |
|---|---|---|
| 纯问候、能力介绍或使用引导 | `greeting` | 问候回复模板 |
| 查询个人指标 | `data_analysis` | 数据分析 Agent |
| 分析、对比或趋势 | `data_analysis` | 数据分析 Agent |
| 根据个人数据给建议 | `data_based_advice` | 数据分析 Agent -> 业务建议 Agent |
| 不依赖个人数据的饮食/运动建议 | `domain_advice` | 业务建议 Agent |
| 非业务问题 | `out_of_scope` | 范围回复模板 |
| 需要医疗谨慎或紧急处理 | `safety` | 安全回复模板 |
| 关键参数不足或置信度过低 | `clarification` | 澄清模板 |

### 主 Agent 的确定性护栏

模型计划返回后，必须通过代码护栏：

- `route` 和 `intent_result.intent` 必须匹配允许的映射。
- `required_agents` 必须与路由表一致。
- `greeting`、`safety`、`out_of_scope`、`clarification` 路由不得访问指标 Repository。
- 只有 `data_analysis` 和 `data_based_advice` 可以访问个人数据。
- 业务建议 Agent 只能接收 `AnalysisResult`、`AdviceFacts` 或领域知识上下文。
- 任意模型输出校验失败时，进入安全的错误或澄清分支，不自动执行未知工具。

### 主 Agent 的失败策略

1. 意图模型超时：使用规则分类器处理明显请求；无法确定时澄清。
2. 结构化输出失败：重试一次，仍失败则不调用专业 Agent。
3. 专业 Agent 超时：发送 `warning`，返回已完成的结构化结果或安全降级文案。
4. 用户中途断开：取消当前工作流和下游请求。
5. 未知异常：发送统一 `error` 事件，不返回内部堆栈和提示词。

## 8.2 短期会话记忆与多轮对话

Chat 支持多轮对话，但一期只提供会话级短期记忆，不建立跨会话的长期用户记忆或用户画像。

### 记忆范围

会话记忆通过 `conversation_id` 关联，并且必须同时校验 `user_id`，防止不同用户读取同一个会话：

- 当前会话的用户消息和助手最终答复。
- 已确认的指标、时间范围、时区、对比方式和建议目标（`confirmed_entities`）。
- 最近一次意图结果和未完成的澄清上下文（`last_intent` + `pending_entities`）。
- 一份长度受限的会话摘要，用于较长会话的后续轮次。

澄清轮次也写入记忆：用户消息和澄清问题作为轮次保存，未确认的槽位写入
`pending_entities`。下一轮短回答（如“体重”“最近三个月”）由规则分类器合并进待定槽位；
只有回答携带新的查询/分析/建议诉求时，才按新意图路由。

不保存模型内部推理过程、无关客户端数据、未经必要性判断的完整数据库结果，或跨会话复用的敏感健康画像。

### 多轮示例

```text
用户：分析我最近一个月的体重
助手：最近 30 天体重下降 1.2 kg，趋势稳定。

用户：那饮食上怎么调整？
```

第二轮通过会话记忆得到最近确认的指标、时间范围和 `AnalysisResult`，因此可以路由为：

```text
data_based_advice -> 使用已有 AnalysisResult/AdviceFacts -> 业务建议 Agent
```

如果上一轮没有成功完成分析，或上下文已经过期，则必须重新查询或向用户澄清，不能假设记忆仍然有效。

### 记忆接口

```python
class ConversationMemory(Protocol):
    async def load(self, user_id: str, conversation_id: str) -> ConversationContext | None: ...
    async def append_turn(self, user_id: str, conversation_id: str, turn: ConversationTurn) -> None: ...
    async def update_context(self, user_id: str, conversation_id: str, context: ConversationContext) -> None: ...
    async def delete(self, user_id: str, conversation_id: str) -> None: ...
```

建议的上下文结构：

```python
class ConversationContext(BaseModel):
    conversation_id: str
    user_id: str
    summary: str | None
    recent_turns: list[ConversationTurn]
    confirmed_entities: QueryEntities | None
    last_intent: ChatIntent | None
    last_analysis: AnalysisResult | None
    expires_at: datetime
```

### 生命周期和容量

一期建议默认：空闲 TTL 24 小时、最多保留 10 个完整轮次、摘要最多 2,000 个字符。超过容量时优先摘要旧轮次，不扩大上下文窗口；收到新请求时续期，过期会话视为新会话。

### 请求生命周期

```text
认证 -> 校验 conversation_id 所属用户 -> load ConversationContext
  -> Supervisor 读取摘要和最近轮次 -> 执行专业 Agent
  -> SSE done 后保存最终轮次和已确认上下文 -> 更新 TTL
```

只有请求正常完成或生成了明确的澄清/安全回复时才写入最终助手轮次。当前 `ChatWorkflow` 已接入 `ConversationMemory`：请求开始加载短期上下文，意图识别使用最近轮次和已确认实体，请求完成后保存用户消息、助手答复、最近意图和实体。客户端中途断开时，不得把不完整的模型增量当作最终答复。

### 存储实现

- 单进程开发和测试：`InMemoryConversationMemory`。
- 多实例部署：Redis，键格式为 `conversation:{user_id}:{conversation_id}`，设置 TTL。
- 关系数据库只在需要审计、历史查询或合规保留时保存最终轮次，不作为每个图节点的实时共享状态。

LangGraph checkpoint 与会话记忆不是同一个概念：checkpoint 用于恢复图执行，ConversationMemory 用于支持用户下一轮对话。一期先实现会话记忆，不启用跨请求 checkpoint。

### 会话安全

- 不属于当前用户的 `conversation_id` 不得泄露是否存在。
- 日志只记录匿名会话 ID 和事件状态，不记录完整健康数据。
- 删除会话必须同时删除内存/Redis 内容和可选的持久化轮次。
- 会话摘要生成后需要经过长度、敏感字段和提示注入检查。

## 9. LangGraph 节点设计

### 9.1 节点与职责

| 节点 | 类型 | 职责 | 失败策略 |
|---|---|---|---|
| `greeting_reply` | 模板 | 回复纯问候、能力介绍和使用引导 | 无模型依赖 |
| `load_memory` | 确定性工具 | 按 `user_id + conversation_id` 读取短期会话上下文 | 不可用时降级为无记忆的单轮请求 |
| `guard_request` | 确定性 + 可选模型 | 检查空输入、提示注入、紧急健康风险 | 高风险进入安全回复；格式错误终止 |
| `understand_request` | 规则 + LLM 结构化输出 | 识别领域、主意图、输出需求、实体、风险和置信度 | 结构化校验失败重试一次，再用规则兜底 |
| `normalize_query` | 确定性 | 解析时区、时间范围、指标别名和默认对比周期 | 参数不足时生成澄清问题 |
| `query_metrics` | 工具 | 通过受控 Repository 查询用户数据 | 数据库瞬时错误有限重试 |
| `analyze_metrics` | 确定性 | 计算统计、趋势、周期变化和数据完整性 | 计算错误终止；数据少则 warning |
| `evaluate_rules` | 确定性 | 生成有编号、可追溯的建议事实 | 缺规则时降级为一般建议素材 |
| `compose_answer` | LLM | 仅根据状态中的事实和来源组织回答 | 一次重试；失败返回结构化数据摘要 |
| `validate_answer` | 确定性 + 可选模型 | 核验数字、引用、医疗边界和输出范围 | 删除无依据内容或使用安全模板 |
| `scope_reply` | 模板 | 回复非业务问题 | 无模型依赖 |
| `safety_reply` | 模板 | 紧急或高风险健康场景提示 | 无模型依赖 |
| `clarification_reply` | 模板 | 关键实体缺失或意图置信度不足时提问 | 不调用数据库 |
| `save_memory` | 确定性工具 | 保存完整轮次、已确认实体和必要分析结果并续期 TTL | 保存失败不改变已生成答复，记录告警 |

### 9.2 图路由

```text
START
  -> load_memory
  -> guard_request
      -> safety_reply -> END
      -> understand_request
          -> greeting -> greeting_reply -> END
          -> out_of_scope -> scope_reply -> END
          -> needs_clarification -> clarification_reply -> END
          -> urgent -> safety_reply -> END
          -> metric_query -> normalize_query -> query_metrics
          -> metric_analysis -> normalize_query -> query_metrics
          -> data_based_advice -> normalize_query -> query_metrics
          -> domain_advice -> business_advice

query_metrics
  -> 仅查询 -> compose_answer
  -> 需要比较/建议 -> analyze_metrics -> evaluate_rules -> compose_answer

evaluate_rules -> compose_answer
business_advice -> compose_answer
compose_answer -> validate_answer -> save_memory -> END
```

当前 Workflow 已通过 `route_execution` SSE `data` 事件发布节点执行结果和执行顺序；注入 `MetricRepository` 后，数据分析节点会执行查询并调用确定性 `MetricAnalysisService` 生成 `AnalysisResult`。未注入仓库时仍只返回查询计划。`data_based_advice` 会把分析结果作为结构化事实传给业务建议节点。

指标分析采用“通用统计内核 + 指标策略注册表”：几十个指标共享观测数、首末值、最大/最小值、平均值、变化量、变化率和趋势计算；需要不同最少样本数、稳定阈值或特殊派生逻辑的指标通过 `MetricAnalysisSpec`/扩展策略注册，不在主流程中堆叠指标名称分支。`AnalysisResult.metadata` 用于承载可扩展的指标特有事实。

当前已建立 `MetricCatalog` 作为指标元数据入口，首批覆盖 `weight`、`bmi`、`body_fat_rate`、`waist_circumference`、`basal_metabolic_rate`、`muscle_mass` 和 `body_water_rate`。每个指标集中维护展示名、默认单位、最少样本数、稳定阈值及是否支持建议；后续增加指标时优先扩展目录和专属策略，不修改 Chat 主流程。

`route_execution` 至少包含 `route`、`status`、`timezone`、`node_results` 和 `data`。数据型路由的 `data` 中包含时间解析结果与查询计划；最近 N 条请求使用 `latest_count/latest_query`，周期请求使用 `time_resolution/metric_query`。

### 9.3 是否需要任务拆分

“分析最近一个月的数据，并给出饮食建议”需要拆分，但拆分为固定、可观测的图节点，而不是让 Planner 自由生成任意任务：

1. 解析“最近一个月”和用户时区。
2. 确定默认对比基线为之前连续 30 天。
3. 查询两个周期的数据。
4. 计算统计、趋势和数据质量。
5. 应用健康建议规则。
6. 生成并校验答复。

只有未来出现大量动态工具、任务顺序无法预先定义时，才考虑增加 Planner 节点。

## 10. 数据查询与对比约定

### 10.1 时间语义

- “最近 7 天/30 天/一个月”：截至用户当地时间的当前日期，使用滚动周期。
- “本周/上周”：按用户当地时间的自然周，周一为首日。
- “本月/上个月”：按用户当地时间的自然月。
- 用户明确给出起止日期时，优先使用用户范围。
- 请求中的模糊时间必须归一化为闭区间日期，并在 `data` 事件中返回实际范围。

### 10.2 默认对比

- 用户要求“分析”但未指定基线时，默认与紧邻的等长上一周期比较。
- 用户只要求“查询”时，不自动执行对比。
- 数据少于 3 个有效观测点时，不输出趋势结论。
- 数据覆盖率不足时返回 `warning`，建议中说明不确定性。

### 10.3 统计职责

分析服务负责计算，而不是 LLM：

- 首值、末值、最小值、最大值、平均值和中位数。
- 绝对变化、变化率和日均/周均变化速度。
- 线性趋势方向及斜率。
- 目标区间达标率。
- 数据覆盖率和最长缺失区间。
- 与基线周期的差异。

### 10.4 数据库未定时的领域契约

当前只确定数据来自数据库，物理表结构和数据库类型尚未确定。V1 先定义 Chat 所需的最小领域模型，数据库层负责把实际字段映射为该模型。

```python
class MetricType(str, Enum):
    WEIGHT = "weight"
    BMI = "bmi"
    BODY_FAT_RATE = "body_fat_rate"
    WAIST_CIRCUMFERENCE = "waist_circumference"


class MetricObservation(BaseModel):
    metric: MetricType
    value: Decimal
    unit: str
    measured_at: datetime
    source: str | None = None


class MetricQuery(BaseModel):
    user_id: str
    metrics: set[MetricType]
    start_at: datetime
    end_at: datetime
    timezone: str


class MetricResult(BaseModel):
    observations: list[MetricObservation]
    available_metrics: set[MetricType]
    queried_at: datetime
```

约束：

- `measured_at` 在服务内部统一为带时区的 UTC 时间，展示和自然日计算时转换到用户时区。
- `value` 使用 `Decimal`，避免关键数值在数据库与 Python 间产生不必要的浮点误差。
- Repository 返回统一标准单位；原始单位换算在数据库适配层完成。
- `source` 只标识数据来源类型，例如 `manual`、`scale`、`device_api`，不暴露设备密钥。
- 领域模型不包含数据库主键、表名、ORM 关系或供应商字段。

首期指标枚举仍需业务确认。在确认之前，不应将任意字符串指标直接透传到 SQL。

### 10.5 Repository 契约

LangGraph 的 `query_metrics` 节点只依赖抽象接口：

```python
class MetricRepository(Protocol):
    async def list_available_metrics(self, user_id: str) -> set[MetricType]: ...

    async def get_observations(self, query: MetricQuery) -> MetricResult: ...

    async def get_latest_observations(
        self,
        user_id: str,
        metrics: set[MetricType],
        before: datetime | None = None,
    ) -> list[MetricObservation]: ...
```

设计要求：

- 所有方法必须显式携带 `user_id`，查询条件必须强制按用户隔离。
- Repository 不接收自然语言、不接收 LLM 生成的 SQL，也不返回 ORM 实例。
- 查询参数先经过指标白名单和最大时间范围校验。
- 数据库异常转换为领域错误，例如 `DataSourceUnavailable`，不向图泄漏驱动异常。
- 单次查询返回数量设置上限；长周期分析优先由 SQL 聚合或分桶。
- Repository 的排序约定为 `measured_at` 升序，并明确重复时间点的处理策略。

### 10.6 数据库适配策略

数据库表确定后新增一个实现，例如 `SqlMetricRepository`：

```text
LangGraph query_metrics 节点
        -> MetricRepository 接口
            -> SqlMetricRepository
                -> ORM/SQL 查询
                    -> 实际数据库表
```

适配过程只需要完成：

1. 将实际用户字段映射到 `user_id`。
2. 将指标字段或指标行映射到 `MetricType`。
3. 将原始值和单位转换为标准单位。
4. 将数据库时间转换为 UTC aware datetime。
5. 补充 Repository 集成测试和典型查询索引。

在表结构确定前，可以使用 `InMemoryMetricRepository` 和固定测试数据并行开发 Chat schema、SSE、LangGraph 路由及分析服务。该实现只用于测试和本地演示，不进入生产环境。

### 10.7 对未来表结构的最低要求

无论最终采用宽表还是指标明细表，数据库至少需要表达：

- 数据所属用户。
- 指标类型或可映射的指标字段。
- 指标值和单位。
- 实际测量时间。
- 可选的数据来源。
- 创建时间；如允许修改历史数据，还需要更新时间。

建议建立覆盖 `user_id + metric + measured_at` 的查询索引。若采用每个指标独立列的宽表，则根据实际查询方式为 `user_id + measured_at` 建立复合索引。

## 11. 建议生成约束

建议链路为：

```text
查询事实 -> 分析事实 -> 规则命中 -> 建议素材 -> LLM 表达 -> 结果校验
```

每条规则至少包含：

- `rule_id` 和版本。
- 适用指标和人群条件。
- 可执行的确定性条件。
- 风险级别。
- 建议素材和禁用表述。
- 规则依据或维护来源。

LLM 不得：

- 修改统计结果或编造缺失数据。
- 将相关性表述为医学诊断。
- 推荐处方药、调整药量或替代医生意见。
- 在没有检索来源时生成虚假引用。

### 11.1 健康建议 Agent 的阶段性设计

数据库表结构和数据适配器尚未确定，因此暂时暂停 `data_analysis` 的真实数据闭环，
但保留数据分析 Agent 的输入输出契约，不删除后续接入点。健康建议 Agent 分为两个入口：

1. **领域型建议（`domain_advice`）**：不读取个人历史数据，直接处理减脂饮食、
   早餐搭配、运动、睡眠和生活方式等业务问题。这部分可以在当前阶段先实现。
2. **数据型建议（`data_based_advice`）**：必须接收 `AnalysisResult` 或
   `AdviceFacts` 后才能给出针对个人数据的建议。在数据库未接入、没有分析事实时，
   不得生成推测性结论，应返回“缺少数据”的受控结果，建议增加独立的 `needs_data`
   状态，避免与用户表达不清的 `needs_clarification` 混淆。

健康建议 Agent 不直接查询数据库，也不负责计算指标。建议采用以下固定流水线：

```text
安全检查
  -> 建议上下文构建
  -> 确定性健康规则
  -> LLM 组织表达（一期不联网）
  -> 结构化结果校验
```

输入至少包括用户问题、建议类别、目标、时区、会话上下文，以及可选的
`AnalysisResult`/`AdviceFacts`。输出应包含摘要、可执行建议、建议类别、依据事实、
风险提示、缺失信息和规则版本。LLM 只能组织已经允许的建议素材，不能编造用户数据、
诊断、药物方案或外部引用。

第一阶段先实现 `domain_advice` 的规则版和供应商无关的结构化 LLM 适配协议；
`data_based_advice` 保持路由和契约不变，暂时在缺少分析事实时安全降级。这样可以先验证
健康建议 Agent 的输入输出、安全边界和多轮对话行为，而不提前绑定数据库结构。

当前代码通过 `RuleFirstHealthAdviceAgent` 先生成规则基线；可选的
`LlmHealthAdvicePolisher` 只能修改摘要、标题和措辞，建议数量、类别、依据、风险等级、
警告和规则版本均由服务端保留并校验。模型调用失败或输出越权时回退到规则基线。

## 12. 联网能力与安全边界

### 12.1 一期不联网的决策

一期不调用搜索引擎、网页抓取或外部健康知识 API。领域型建议使用模型自身能力、版本化业务规则和审核后的提示模板完成。

这样做的主要原因：

- C 端体验优先，减少外部网络带来的首字节延迟和超时。
- 避免网页内容质量、广告、版权和来源可信度治理成为一期阻塞点。
- 饮食建议的主要场景是稳定的体重管理知识，不要求实时资讯。
- 保持 SSE 流程简单，先验证用户是否真正需要实时资料引用。

模型回答中不得伪造论文、机构、URL 或“最新指南”引用。用户明确要求最新研究、政策或指南时，一期应诚实说明当前不提供实时检索，并给出通用范围内的谨慎回答或建议咨询专业人士。

### 12.2 后续联网扩展点

未来如确有需要，新增独立的 `knowledge_retrieval` 子图或工具，不改变 `domain_advice` 和 `data_based_advice` 的输入输出契约。接入前需要单独评估来源白名单、缓存、超时、引用校验和隐私边界。

### 12.3 安全升级

当用户描述急症信号、自伤风险或明显需要诊疗的情况时，停止普通建议流程，返回安全模板，说明应联系当地急救服务或专业医疗人员。具体触发词和策略需要医学审核。

规则分类器内置分层安全召回，作为 LLM 结果之上的强制护栏（规则命中风险时，混合分类器不再调用 LLM）：

1. **分层词表**：`URGENT` 覆盖急症与自伤信号（胸痛、呼吸困难、喘不上气、窒息、昏迷、心脏骤停、心梗、中风、脑出血、大出血、中毒、休克、自杀、自伤、急救等）；`MEDICAL_REVIEW` 覆盖慢性病、用药与诊疗信号（糖尿病、高血压、低血糖、心脏病、心慌、心悸、癌症、肿瘤、抑郁、处方药、减肥药、药量、停药、胰岛素、诊断、确诊、治疗、手术、水肿、便血、尿血、咳血、高烧等）。
2. **否定感知匹配**：风险词前 4 个字符内出现“不/没/无/未”视为否定表达，不触发风险（如“我没有胸痛”“不是高血压”“无用药史”）。否定仅按最近邻窗口判断，跨句否定需要医学审核后人工补词。
3. **部位+症状组合**：词表无法穷举的组合由确定性正则召回，覆盖胸口/心脏/头/脑/腹/胃/腿/腰等部位与痛/疼/闷/晕/麻/胀/酸/出血/发烧/不舒服等症状（正向与倒装语序），否定词位于组合中间或前方时同样跳过（如“胸口不疼”）。
4. **严重程度升级**：关键部位（胸口/心脏/心口/头/脑）的组合症状若伴随程度词（剧烈、持续、严重、难以忍受、突然、不停、越来越、长期）升级为 `URGENT`；其他部位保持 `MEDICAL_REVIEW`。
5. **失眠特例**：失眠、睡不着、入睡困难等词伴随程度词（严重/长期/持续/一直/整夜/彻夜）时进入 `MEDICAL_REVIEW`；普通睡眠咨询仍走建议路由（睡眠词已并入建议词表）。
6. **孤立症状兜底**：无业务诉求信号（指标/查询/分析/测量）的孤立症状描述（发烧、乏力、恶心、腹泻、麻木等）进入 `MEDICAL_REVIEW`；“心疼/心痛”作为情绪表达先剔除，“好心疼”不会误触发。
7. **阻断状态**：安全回复节点返回 `status=blocked`，SSE 以 `done(status=blocked)` 结束，客户端可据此区别于正常完成的轮次做特殊展示。

已知取舍：规则护栏偏向召回（宁多勿漏），隐喻表达（如“这个 bug 让人头疼”）与程度否定（如“不是很疼”）可能造成误召回或漏召回；后者由 LLM 主分类器兜底。疾病词优先于范围外判断（“我有高血压，帮我写诗”走医疗风险分支而非范围外回复）。

## 13. 错误模型

统一错误码建议：

| 错误码 | 场景 | 是否可重试 |
|---|---|---:|
| `INVALID_REQUEST` | 请求字段或时间范围无效 | 否 |
| `AUTH_REQUIRED` | 缺少或无效身份 | 否 |
| `CLARIFICATION_REQUIRED` | 关键参数无法可靠推断 | 否，需用户补充 |
| `DATA_NOT_FOUND` | 时间范围内没有数据 | 否 |
| `DATA_INSUFFICIENT` | 数据可查但不足以分析 | 否 |
| `DEPENDENCY_TIMEOUT` | 数据库、模型或搜索超时 | 是 |
| `MODEL_OUTPUT_INVALID` | 模型结构化结果校验失败 | 是，服务端有限重试 |
| `INTERNAL_ERROR` | 未分类内部错误 | 视情况 |

错误响应不得包含堆栈、SQL、模型密钥或内部提示词。

## 14. 会话与持久化边界

V1 已确定使用带 TTL 的短期会话记忆支持多轮对话；它不等同于长期聊天历史或 LangGraph checkpoint。

最低要求：

- 保存用户消息、最终助手消息、时间戳和会话 ID。
- 保存本次使用的时间范围、指标、规则版本和引用 URL，便于审计。
- 不保存模型思维过程。
- 日志中对用户健康数据做最小化记录和必要脱敏。

待决定：

- 对话历史保留期限。
- 是否允许用户删除单条消息或整个会话。
- 多轮对话使用 `ConversationMemory`，不启用跨请求 LangGraph checkpoint。
- 会话标题生成方式。

## 15. 建议代码结构

```text
src/weight_agent/
  api/
    routes/chat.py
    schemas/chat.py
    sse.py
  chat/
    graph.py
    state.py
    routing.py
    nodes/
      guard.py
      understand.py
      normalize.py
      query_metrics.py
      analyze.py
      evaluate_rules.py
      search.py
      compose.py
      validate.py
  domain/
    metrics/models.py
    metrics/service.py
    advice/models.py
    advice/rules.py
  repositories/
    metric_repository.py
    conversation_repository.py
  integrations/
    llm/client.py
    search/client.py
  core/
    config.py
    errors.py
    logging.py
```

约束：API schema、图状态、领域模型和数据库模型必须分离，避免一个 Pydantic 模型承担所有层的职责。

## 16. 非功能要求

### 性能目标

- 建连后 1 秒内发送 `start`。
- 无联网的查询类请求，P95 首个有效内容事件小于 3 秒。
- 一期不包含联网请求；二期如启用联网，再单独设定检索超时和首字节目标。
- 单次 Chat 请求设置总超时；具体数值在模型和搜索供应商确定后压测设定。

### 可观测性

每次请求至少记录：

- `request_id`、`conversation_id`、用户匿名标识。
- 图节点开始、结束、耗时和结果状态。
- 数据库、LLM、搜索调用耗时和错误类型。
- 模型名称、提示模板版本和 token 用量。
- 不记录完整健康数据和模型密钥。

### 测试

- 单元测试：时间解析、统计计算、规则命中和 SSE 编码。
- 契约测试：请求 schema、事件顺序和错误码。
- 图测试：每一种意图分支、降级和重试路径。
- 集成测试：数据库查询、模型结构化输出和断开取消。
- 安全测试：跨用户访问、提示注入、虚假引用和医疗越界。

## 17. 实施计划与进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 服务骨架、Python 3.11、基础测试 | 已完成 |
| 1 | Chat 需求边界、接口和图设计 | 一期基线已完成，随实现持续更新 |
| 2 | Chat schema、SSE 基础设施、可替换工作流、内存数据适配器 | 已完成（占位实现） |
| 3 | 意图识别、时间归一化和路由 | 已完成规则优先的混合意图识别，并已接入 ChatWorkflow；真实 LLM Client、时间归一化与 Supervisor 待开发 |
| 3a | Chat Supervisor 职责、输入输出契约和确定性护栏 | 已实现确定性 SupervisorPlan 生成、模型护栏和 Workflow 接入 |
| 3c | Chat 节点协议与一期节点实现 | 已完成统一 NodeContext/NodeResult 协议、模板回复节点和业务占位节点 |
| 3b | 短期会话记忆与多轮对话 | 已完成内存版契约和测试，Redis 适配待后续实现 |
| 4 | 指标 Repository、查询与分析服务 | 已完成 Repository 注入、MetricQuery 执行和 MetricAnalysisService；数据库适配器待表结构确定后实现 |
| 5 | 健康建议 Agent、规则引擎与建议生成 | 已完成规则版 `domain_advice` 和结构化 LLM 适配协议；具体模型服务与数据型建议等待后续 |
| 6 | 可选联网健康检索、引用与安全策略 | 后续评估，不属于一期 |
| 7 | 会话持久化、可观测性和端到端测试 | 已完成内存会话接入和 Chat 主链路测试；Redis 与更完整可观测性待后续 |
| 意图识别深度设计-阶段一 | 新增内部 Task/Topic/Goal/DataDependency、SlotValue、IntentCandidate、SemanticParse 和 DialogueState 模型，保留 `IntentResult` 兼容 | 已完成；尚未接入分类器和会话记忆 |

## 18. 开发前待确认事项

以下事项会影响下一阶段的数据模型和接口实现：

1. 首期支持哪些指标，例如体重、BMI、体脂率、腰围、基础代谢。
2. 数据库类型、表结构、字段定义、单位规则和数据量级。
3. 身高、性别、年龄、目标体重等用户资料由哪个服务提供。
4. 首期接入哪个 LLM，是否需要兼容多个模型供应商。
5. 是否需要二期联网搜索，以及搜索供应商和可信来源白名单。
6. 用户认证方式及 `user_id` 如何注入请求上下文。
7. 是否需要保存多轮会话，以及数据保留期限。
8. 健康规则由谁审核和维护。

## 19. 变更记录

| 日期 | 版本 | 变更 | 决策人 |
|---|---|---|---|
| 2026-09-20 | v0.1 | 建立 Chat V1 总体架构、接口、SSE 和 LangGraph 设计基线 | 待确认 |
| 2026-09-20 | v0.2 | 确认数据来自数据库；增加数据库无关领域模型、Repository 契约和未知表结构适配策略 | 待确认 |
| 2026-09-20 | v0.3 | 实现 Chat 请求模型、SSE 事件流、可替换工作流和内存指标 Repository；补充接口契约测试 | 待确认 |
| 2026-09-20 | v0.4 | 完成分层意图模型、置信度/澄清策略、规则兜底和意图评估集设计 | 待确认 |
| 2026-09-20 | v0.5 | 确定一期不联网；领域建议使用模型能力和审核规则，联网能力延后评估 | 待确认 |
| 2026-09-20 | v0.6 | 完成 Chat Supervisor 主 Agent 的职责、路由表、输入输出契约和失败策略设计 | 待确认 |
| 2026-09-20 | v0.7 | 增加带 TTL 的短期会话记忆、多轮上下文契约和 load/save 节点设计 | 待确认 |
| 2026-09-21 | v0.8 | 实现 ConversationMemory 协议和 InMemoryConversationMemory，增加用户隔离、TTL、轮次上限和删除测试 | 待确认 |
| 2026-09-21 | v0.9 | 实现 IntentClassifier 协议和 ClassifierContext，增加结构化实现与上下文边界测试 | 待确认 |
| 2026-09-21 | v1.0 | 实现 RuleBasedIntentClassifier，覆盖查询、分析、建议、范围外和风险分支，并增加多轮追问测试 | 待确认 |
| 2026-09-21 | v1.1 | 实现 LlmIntentClassifier 和 StructuredOutputClient 协议，增加 JSON Schema、Pydantic 校验及非法输出测试 | 待确认 |
| 2026-09-21 | v1.2 | 实现 HybridIntentClassifier，增加风险优先、LLM 失败回退和低置信度澄清策略 | 待确认 |
| 2026-09-22 | v1.3 | 重构 HybridIntentClassifier 为“安全规则强制优先、明确业务规则优先、复杂语义交给 LLM”的混合识别策略 | 待确认 |
| 2026-09-22 | v1.4 | 将 HybridIntentClassifier 接入 ChatWorkflow，通过 SSE 返回 `data(type=intent_result)`，并支持澄清分支结束状态 | 待确认 |
| 2026-09-22 | v1.5 | 实现确定性 ChatSupervisor，将 `IntentResult` 转换为 `SupervisorPlan`，并通过 SSE 返回 `data(type=supervisor_plan)` | 待确认 |
| 2026-09-22 | v1.6 | 增加 `greeting` 意图与路由，纯问候走模板回复；复合问候加业务请求仍进入业务路由 | 待确认 |
| 2026-09-22 | v1.7 | 新增 `chat/node.py`，定义 ChatNode 协议、NodeContext/NodeResult，以及问候、澄清、安全、范围、数据分析和业务建议节点 | 待确认 |
| 2026-09-22 | v1.8 | 新增 `RouteExecutor` 并接入 ChatWorkflow，固定执行路由节点；`data_based_advice` 按数据分析后业务建议顺序执行 | 待确认 |
| 2026-09-22 | v1.9 | 为 `DataAnalysisNode` 和 `RouteExecutor` 预留 `MetricRepository` 注入口，数据库未就绪时保持占位执行 | 待确认 |
| 2026-09-22 | v1.10 | 统一服务、Chat、Report 和模型服务配置分组，新增 Chat 意图阈值与会话参数的 `.env` 配置 | 待确认 |
| 2026-09-22 | v1.11 | 稳定时间表达解析：支持滚动周期、自然周期、明确日期区间及最近 N 条记录，并区分 `latest_count` 与日期范围 | 待确认 |
| 2026-09-22 | v1.12 | 完成请求时区到 NodeContext、MetricQuery 和 route_execution 的传递，补齐默认时区和最近 N 条查询计划输出 | 待确认 |
| 2026-09-22 | v1.13 | 完成 MetricAnalysisService、Repository 查询接入、分析事实传递和 ChatWorkflow 短期会话记忆接入 | 待确认 |
| 2026-09-22 | v1.14 | 将指标分析扩展为通用统计内核 + MetricAnalysisRegistry 策略注册，支持几十个指标渐进式接入 | 待确认 |
| 2026-09-22 | v1.15 | 建立 MetricCatalog，首批登记 7 个指标并补充指标目录、策略覆盖和全量测试记录 | 待确认 |
| 2026-09-22 | v1.16 | 明确数据库未就绪阶段先实现领域型健康建议 Agent，数据型建议保留契约并在缺少分析事实时安全降级 | 待确认 |
| 2026-09-22 | v1.17 | 实现 `HealthAdviceAgent` 协议、规则版健康建议、`domain_advice` 路由接入和 `needs_data` 安全降级 | 待确认 |
| 2026-09-22 | v1.18 | 增加结构化 LLM 健康建议适配协议，模型调用失败时自动回退规则版建议 | 待确认 |
| 2026-09-22 | v1.19 | 增加规则优先编排器和受约束的 LLM 润色器，禁止模型修改建议事实和安全字段 | 待确认 |
| 2026-09-22 | v1.20 | 将默认 SSE 调整为文本片段流，仅在调试配置开启时发送意图、路由和节点执行事件 | 待确认 |
| 2026-09-22 | v1.21 | 将 `delta` 改为逐文本片段输出，默认 SSE 只保留 `start`、`delta` 和 `done` | 待确认 |
| 2026-09-22 | v1.22 | 精简公共 SSE 负载：移除请求 ID、序号、时间戳及内部字段；调试模式保留完整元数据 | 待确认 |
| 2026-09-22 | v1.23 | 将规则版 SSE 从逐字符改为按短句/适中长度分片，增加可配置分片大小 | 待确认 |
| 2026-09-22 | v1.24 | 接入 DashScope 结构化模型客户端：规则优先的 LLM 意图识别与健康建议安全润色，分别配置模型、超时和输出参数 | 待确认 |
| 2026-09-22 | v1.25 | 调整健康建议架构：通用建议由 LLM 优先生成，规则仅作为安全兜底；修正“体重下降，应该怎么增重”被误路由到数据分析的问题 | 待确认 |
| 2026-09-23 | v1.26 | 修复澄清回路死循环：澄清轮次写入短期记忆并保存待定槽位 `pending_entities`，规则分类器支持澄清回答槽位合并与裸指标词查询，时间正则支持“最近三个月” | 待确认 |
| 2026-09-23 | v1.27 | 修复多轮实体不合并与被覆盖：规则分类器在查询/分析/数据型建议分支合并 `confirmed_entities`；记忆层仅数据类意图携带显式指标时更新确认实体，问候、建议等轮次不再清空上下文 | 待确认 |
| 2026-09-23 | v1.28 | 修复流中途异常断流：工作流异常边界统一转换为终止性 `error` 事件，公共模式放行并只暴露 `code/message/retryable`；时区在 Schema 层校验（复用 tzdata 兜底逻辑），`X-User-Id` 限制长度，日期区间倒置改为澄清 | 待确认 |
| 2026-09-23 | v1.29 | 完善安全召回：急症/医疗词表分层扩充，否定感知匹配，部位+症状组合与关键部位严重程度升级，失眠特例与孤立症状兜底，安全回复以 `done(status=blocked)` 结束 | 待确认 |
| 2026-09-23 | v1.30 | 新增意图识别深度设计：引入内部语义层、任务/主题/目标/数据依赖、槽位元信息、DST、多候选融合、澄清回路、OOS、鲁棒性、评估集和分阶段实施计划 | 待确认 |
| 2026-09-23 | v1.31 | 明确模型使用边界：项目不训练、不微调、不部署本地模型，意图识别、健康建议和报告生成等模型能力统一通过外部模型 API 调用 | 待确认 |
| 2026-09-23 | v1.32 | 在 `chat/models.py` 新增内部语义枚举、槽位元信息、候选意图、`SemanticParse` 和 `DialogueState`；保留现有 `IntentResult` 与路由契约，补充模型校验测试 | 待确认 |
| 2026-09-23 | v1.33 | 明确多语言策略：区分客户端 `locale`、本轮 `detected_language` 和 `response_language`；按本轮语言直接生成答复，不增加独立翻译 API；定义保留原文的多语言文本归一化边界 | 待确认 |
