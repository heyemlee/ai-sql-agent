from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "anthropic"]

# 默认取每家最便宜的在产型号：主链路一次查询要打 5 次模型，
# 默认值贵一档，跑一轮 benchmark 的成本就差出一个量级。
# 想要更强的 SQL 生成，单独覆盖 AI_SQL_CODER_MODEL 即可（如 gpt-5.6-terra）。
DEFAULT_CHAT_MODEL = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-sonnet-5",
}
DEFAULT_CODER_MODEL = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-sonnet-5",
}


def build_endpoint(base_url: str, path: str) -> str:
    """把 BASE_URL 拼成完整端点，容忍它带不带 `/v1` 后缀。

    OPENAI_BASE_URL 通常写到 `/v1`（https://relay.example.com/v1），
    ANTHROPIC_BASE_URL 通常不带（https://relay.example.com），两种都要能用。
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/{path.lstrip('/')}"


class SqlAgentSettings(BaseSettings):
    """
    全局配置。

    统一收口原先散落在各模块构造函数里的 `os.getenv` 调用：
    - 默认前缀 `AI_SQL_`（例如 `AI_SQL_PGVECTOR_DSN`）；
    - 模型凭证直接认 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` 这类通用命名；
    - 支持项目根目录下的 `.env` 文件。

    通过 `get_settings()` 获取单例，测试里用 `reload_settings()` 重新读取。
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_SQL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # model_provider 是配置项而不是 pydantic 的模型元数据，关掉 model_ 前缀保护
        protected_namespaces=(),
    )

    # ---- 模型厂商选择 ----
    model_provider: Provider = Field(
        default="openai",
        validation_alias=AliasChoices("AI_SQL_MODEL_PROVIDER", "MODEL_PROVIDER"),
    )
    """
    主链路（关键词抽取→CoT 规划→SQL 生成）用哪套协议：

    - `openai`：OpenAI 兼容协议（OpenAI 官方、中转站、阿里云百炼、vLLM、Ollama 都是这套）；
    - `anthropic`：Anthropic Messages 协议（`/v1/messages` + `x-api-key`）。

    只改这一个变量就能整体切换，代码不用动。Embedding / Rerank 始终走
    OpenAI 兼容协议（Anthropic 不提供这两种接口）。
    """

    # ---- OpenAI 兼容通道凭证 ----
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("AI_SQL_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )

    # ---- Anthropic 通道凭证 ----
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_SQL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"
        ),
    )
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com",
        validation_alias=AliasChoices(
            "AI_SQL_ANTHROPIC_BASE_URL", "ANTHROPIC_BASE_URL"
        ),
    )
    anthropic_version: str = "2023-06-01"
    """Messages 接口的 `anthropic-version` 请求头。"""

    max_output_tokens: int = 4096
    """Anthropic Messages 协议要求显式给 `max_tokens`，这里是默认值。"""

    embedding_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_SQL_EMBEDDING_API_KEY", "EMBEDDING_API_KEY"
        ),
    )
    """
    Embedding 服务凭证。留空时回落到 `openai_api_key`。

    单独拆出来是因为 Chat 与 Embedding 可能来自不同厂商——例如主链路走
    Anthropic 时，Embedding 只能另配一家（或不配，主链路本就走本地哈希向量）。
    """

    rerank_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_RERANK_API_KEY", "DASHSCOPE_API_KEY"),
    )
    """Rerank（阿里云百炼 qwen3-rerank）凭证，留空时回落到 `openai_api_key`。"""

    dashscope_workspace_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AI_SQL_DASHSCOPE_WORKSPACE_ID", "DASHSCOPE_WORKSPACE_ID"
        ),
    )
    """配了才启用真实 Rerank，否则字段级精排走本地词面打分。"""

    # ---- 模型服务地址与模型名 ----
    chat_url: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_CHAT_URL", "MODEL_CHAT_URL"),
    )
    """
    Chat 端点。留空时按 `model_provider` 从 BASE_URL 自动拼：

    - openai：    `{OPENAI_BASE_URL}/chat/completions`
    - anthropic： `{ANTHROPIC_BASE_URL}/v1/messages`

    显式配置时优先级最高，用于内网 vLLM 这类非标准路径。
    """
    embedding_url: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_EMBEDDING_URL", "MODEL_EMBEDDING_URL"),
    )
    """
    Embedding 端点，留空时取 `{OPENAI_BASE_URL}/embeddings`。

    只有 pgvector 向量索引这条路才会用到；主链路（检索→规划→生成）用的是
    本地哈希向量，不调这个接口，因此只跑 benchmark 时无需配置。
    """
    rerank_url: str = Field(
        default="",
        validation_alias=AliasChoices("AI_SQL_RERANK_URL", "DASHSCOPE_RERANK_URL"),
    )

    chat_model: str = ""
    """规划/路由用的模型，留空时按厂商取默认值。"""

    coder_model: str = ""
    """SQL 生成用的模型，留空时按厂商取默认值。"""

    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    rerank_model: str = "qwen3-rerank"

    # ---- HTTP 调用治理 ----
    http_timeout_seconds: float = 60.0
    http_max_retries: int = 2
    """可重试错误（429/5xx/超时/连接失败）的额外重试次数，0 表示不重试。"""
    http_backoff_base_seconds: float = 0.5
    http_backoff_max_seconds: float = 8.0

    # ---- 鉴权与数据权限 ----
    auth_mode: Literal["disabled", "api_key"] = "disabled"
    """disabled 仅用于本地开发（启动会打警告）；生产部署设为 api_key。"""

    api_keys: str = ""
    """API Key 配置 JSON 字符串，适合本地或极简部署。"""

    api_keys_file: str = ""
    """API Key 配置文件路径，推荐（K8s Secret / Docker secret 挂载）。"""

    # ---- A2A ----
    a2a_streaming: bool = False
    """
    A2A `message/stream` 开关。关 = Card 报 streaming:false、message/stream 返回
    -32004，调用方退回 message/send（契约 §6.2 的回滚路径）。

    默认关，尽管契约 §6.2 建议默认开——契约 v1.3 把「调用方先补好 -32004 降级分支」
    列为硬阻断：那条分支没上线之前打开这个开关，要关的时候就不是降级而是全线故障。
    调用方那边验证过降级分支真的接得住之后再翻成 True。
    """

    # ---- 限流与配额 ----
    default_rate_limit_per_minute: int = 60
    default_daily_query_quota: int = 0
    """0 表示不限制。"""

    default_daily_token_quota: int = 0

    # ---- Schema 热重建 ----
    schema_refresh_seconds: float = 0.0
    """业务库 Schema 轮询间隔，0 表示不开启（本地开发默认）。生产建议 300。"""

    # ---- 审计 ----
    audit_enabled: bool = True
    audit_db_path: str = "runtime_data/audit.db"
    """未配置 pgvector DSN 时审计落这个 SQLite 文件。"""

    # ---- 熔断 ----
    circuit_breaker_enabled: bool = True
    circuit_failure_threshold: int = 5
    """连续多少次基础设施类失败后跳闸（超时/连接失败/5xx/限流）。"""

    circuit_reset_seconds: float = 30.0
    """跳闸后多久放一次试探请求。"""

    # ---- 业务语义元数据 ----
    semantic_dir: str = ""
    """
    业务语义元数据目录（每个库一个 YAML，见 ai_sql_semantic）。

    留空时使用内置的四个演示库元数据——**接真实业务库必须配置这一项**：
    模型认识业务全靠这份元数据，缺了它检索只能按字段名字面匹配，
    口语问法基本召不回来，现象是「模型答不准」，实际是没告诉它业务是什么。

        python -m ai_sql_semantic export semantic/     # 导出模板
        python -m ai_sql_semantic validate semantic/   # 校验并与真实库比对
        AI_SQL_SEMANTIC_DIR=semantic/
    """

    # ---- 用户反馈 ----
    feedback_enabled: bool = True
    """
    收集用户对答案的对错反馈。

    关掉之后 /feedback 返回 404，认证 SQL 复用与 `python -m ai_sql_feedback`
    也就没有数据来源了。
    """

    certified_sql_enabled: bool = True
    """
    把用户标记「正确」的查询作为认证范例注入生成 Prompt。

    只用同一用户/同一权限下确认过的 SQL，且执行前照常过权限校验。
    """

    certified_sql_top_k: int = 2
    """单次查询最多注入几条认证范例。多了会挤占 Schema 的 Token 预算。"""

    # ---- 存储 ----
    pgvector_dsn: str = ""
    """配置后 Schema 向量索引与长期记忆向量索引都切到 PostgreSQL + pgvector。"""
    memory_db_path: str = "runtime_data/memory.db"

    # ---- SQL 执行治理 ----
    max_rows: int = 10000
    max_execution_seconds: float = 30.0

    # ---- 日志 ----
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @model_validator(mode="after")
    def _fill_provider_defaults(self) -> "SqlAgentSettings":
        """按选中的厂商补齐端点与模型名，显式配置永远优先。"""
        if not self.chat_url:
            if self.model_provider == "anthropic":
                self.chat_url = build_endpoint(self.anthropic_base_url, "messages")
            else:
                self.chat_url = build_endpoint(
                    self.openai_base_url, "chat/completions"
                )
        if not self.embedding_url:
            self.embedding_url = build_endpoint(self.openai_base_url, "embeddings")
        if not self.chat_model:
            self.chat_model = DEFAULT_CHAT_MODEL[self.model_provider]
        if not self.coder_model:
            self.coder_model = DEFAULT_CODER_MODEL[self.model_provider]
        return self

    def resolved_chat_api_key(self) -> str:
        """主链路凭证：按选中的厂商取对应的 Key。"""
        if self.model_provider == "anthropic":
            return self.anthropic_api_key
        return self.openai_api_key

    def resolved_embedding_api_key(self) -> str:
        """Embedding 凭证：单独配了就用单独的，否则回落到 OpenAI 兼容通道。"""
        return self.embedding_api_key or self.openai_api_key

    def resolved_rerank_api_key(self) -> str:
        """Rerank 凭证：单独配了就用单独的，否则回落到 OpenAI 兼容通道。"""
        return self.rerank_api_key or self.openai_api_key

    @property
    def has_model_credentials(self) -> bool:
        return bool(self.resolved_chat_api_key())

    def resolved_rerank_url(self) -> str:
        """Rerank 走业务空间专属域名，未显式配置时按 workspace_id 拼接。"""
        if self.rerank_url:
            return self.rerank_url
        if self.dashscope_workspace_id:
            return (
                f"https://{self.dashscope_workspace_id}"
                f".cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks"
            )
        return ""


@lru_cache(maxsize=1)
def get_settings() -> SqlAgentSettings:
    return SqlAgentSettings()


def reload_settings() -> SqlAgentSettings:
    """清掉缓存重新读取环境变量，供测试与运行期改配置使用。"""
    get_settings.cache_clear()
    return get_settings()
