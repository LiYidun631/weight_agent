"""应用配置模块：集中定义服务的所有可配置项。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """应用配置模型。

    配置项通过环境变量注入，前缀为 WEIGHT_AGENT_，
    也可从项目根目录的 .env 文件中读取。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",  # 始终从项目根目录读取
        env_prefix="WEIGHT_AGENT_",  # 环境变量统一前缀
        extra="ignore",  # 忽略未定义的额外配置项
        populate_by_name=True,  # 允许测试和代码用字段名显式覆盖 .env
    )

    # ==================== 服务基础配置 ====================
    app_name: str = "Weight Agent API"  # 应用名称（用于文档与日志）
    environment: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False  # 是否开启调试模式
    host: str = "127.0.0.1"  # Web 服务监听地址
    port: int = Field(default=8000, ge=1, le=65535)  # Web 服务监听端口
    reload: bool = False  # 是否启用代码热重载
    api_v1_prefix: str = "/api/v1"  # API v1 路由前缀

    # ==================== Chat 接口配置 ====================
    # 意图识别置信度阈值：规则优先，复杂语义再交给 LLM。
    chat_intent_min_confidence: float = Field(default=0.75, ge=0, le=1)
    chat_intent_fallback_confidence: float = Field(default=0.8, ge=0, le=1)
    chat_intent_rule_confidence: float = Field(default=0.84, ge=0, le=1)

    # Chat 短期会话记忆配置；生产多实例部署时替换为 Redis 实现。
    chat_memory_ttl_hours: float = Field(default=24.0, gt=0)
    chat_memory_max_turns: int = Field(default=10, ge=1, le=100)

    # Chat 请求缺省上下文配置。
    chat_default_timezone: str = "Asia/Shanghai"
    chat_default_locale: str = "zh-CN"

    # Chat SSE 是否暴露意图、路由和节点执行等内部调试事件。
    # 生产和普通客户端默认关闭；开发排查时可在 .env 中开启。
    chat_expose_debug_events: bool = False
    # 规则版 SSE 文本分片大小；接入原生流式模型后由模型 chunk 决定。
    chat_stream_chunk_size: int = Field(default=12, ge=1, le=100)
    # Chat 是否启用真实模型；规则分类和规则建议始终保留为兜底。
    chat_llm_enabled: bool = True

    # Chat 意图识别模型：规则无法明确判断时才调用。
    chat_intent_model: str = "qwen3.7-flash"
    chat_intent_model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_intent_model_timeout_seconds: float = Field(default=8.0, gt=0)
    chat_intent_model_temperature: float = Field(default=0.0, ge=0, le=2)
    chat_intent_model_max_completion_tokens: int = Field(default=512, gt=0)
    chat_intent_model_enable_thinking: bool = False

    # Chat 健康建议模型：只润色规则建议，不修改建议事实和安全字段。
    chat_advice_model: str = "qwen3.7-flash"
    chat_advice_model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    chat_advice_model_timeout_seconds: float = Field(default=12.0, gt=0)
    chat_advice_model_temperature: float = Field(default=0.2, ge=0, le=2)
    chat_advice_model_max_completion_tokens: int = Field(default=800, gt=0)
    chat_advice_model_enable_thinking: bool = False

    # ==================== Report 接口配置 ====================
    # 报告模型调用参数；密钥和地址不应写死在业务代码中。
    report_model_timeout_seconds: float = Field(default=20.0, gt=0)  # 模型调用超时（秒）
    report_model: str = "qwen3.8-flash"  # 报告分析使用的模型名称
    report_model_temperature: float = Field(default=0.2, ge=0, le=2)
    report_model_max_completion_tokens: int = Field(default=800, gt=0)
    report_model_enable_thinking: bool = False
    report_model_strict_output_validation: bool = False

    # ==================== 模型服务配置 ====================
    report_model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            # 兼容裸变量名与带前缀变量名两种注入方式
            "DASHSCOPE_API_KEY",
            "WEIGHT_AGENT_DASHSCOPE_API_KEY",
        ),
    )  # DashScope API 密钥；未配置时报告分析走兜底逻辑

    @field_validator("dashscope_api_key")
    @classmethod
    def normalize_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        key = value.get_secret_value().strip()
        return SecretStr(key) if key else None


@lru_cache
def get_settings() -> Settings:
    """获取全局唯一配置实例（进程内缓存，避免重复解析环境变量）。"""

    return Settings()
