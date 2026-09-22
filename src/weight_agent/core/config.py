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

    # ==================== Report 接口配置 ====================
    # 报告模型调用参数；密钥和地址不应写死在业务代码中。
    report_model_timeout_seconds: float = Field(default=45.0, gt=0)  # 模型调用超时（秒）
    report_model: str = "qwen3.8-flash"  # 报告分析使用的模型名称
    report_model_temperature: float = Field(default=0.2, ge=0, le=2)
    report_model_max_completion_tokens: int = Field(default=1200, gt=0)
    report_model_enable_thinking: bool = False

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
