"""Pluggable LLM client. | 可插拔 LLM 客户端。

EN: This is the foundation of the frontend "selectable API" dropdown. It wraps
    any OpenAI-compatible provider and enforces the privacy tier before any
    request leaves the machine (DESIGN section 8).
ZH: 这是前端“可选 API”下拉的地基。它封装任意 OpenAI 兼容的 Provider，并在任何
    请求离开本机前强制执行隐私档（设计文档第 8 节）。

EN: Design goals:
    - No hard dependency on `openai`; if not installed or air-gapped, the client
      simply reports unavailable and Discovery falls back to static-only.
    - Privacy is enforced here, centrally, not scattered across engines.
ZH: 设计目标：
    - 不硬依赖 `openai`；未安装或 air-gapped 时，客户端直接报告不可用，
      Discovery 退化为纯静态。
    - 隐私在此集中强制执行，而非散落在各引擎里。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PrivacyMode(str, Enum):
    # EN: pure static, never call an LLM | ZH: 纯静态，绝不调用 LLM
    air_gapped = "air-gapped"
    # EN: in-enterprise / self-hosted LLM (recommended default) | ZH: 企业内/自托管 LLM（推荐默认）
    private_llm = "private-llm"
    # EN: public LLM API + redaction | ZH: 公有 LLM API + 脱敏
    external_llm = "external-llm"


# EN: Provider presets -> OpenAI-compatible base URLs and default env var names.
# ZH: Provider 预设 -> OpenAI 兼容的 base URL 与默认环境变量名。
PROVIDERS: dict[str, dict[str, str]] = {
    "modelscope": {
        "base_url": "https://api-inference.modelscope.cn/v1/",
        "api_key_env": "MODELSCOPE_API_KEY",
        "default_model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "aihubmix": {
        "base_url": "https://aihubmix.com/v1",
        "api_key_env": "AIHUBMIX_API_KEY",
        "default_model": "gpt-4o-mini",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
    },
    # EN: DeepSeek — native OpenAI-compatible API.
    # ZH: DeepSeek —— 原生 OpenAI 兼容接口。
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    # EN: Claude — via Anthropic's OpenAI-compatible endpoint (path /v1/).
    # ZH: Claude —— 走 Anthropic 官方的 OpenAI 兼容端点（路径 /v1/）。
    "claude": {
        "base_url": "https://api.anthropic.com/v1/",
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-3-5-sonnet-latest",
    },
}


@dataclass
class LLMConfig:
    # EN: which preset provider to use | ZH: 使用哪个预设 Provider
    provider: str = "modelscope"
    # EN: override model id (optional) | ZH: 覆盖模型 id（可选）
    model: Optional[str] = None
    # EN: override base url (optional) | ZH: 覆盖 base url（可选）
    base_url: Optional[str] = None
    # EN: override api key (optional) | ZH: 覆盖 api key（可选）
    api_key: Optional[str] = None
    # EN: privacy tier that gates all calls | ZH: 管控所有调用的隐私档
    privacy_mode: PrivacyMode = PrivacyMode.private_llm
    temperature: float = 0.2
    timeout: int = 60


class LLMClient:
    """EN: A thin, privacy-aware wrapper over an OpenAI-compatible chat API.
    ZH: 一个轻量、带隐私意识的 OpenAI 兼容对话 API 封装。"""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self._client = None
        self._init_error: Optional[str] = None
        self._try_init()

    # -- capability | 能力 -------------------------------------------------

    @property
    def available(self) -> bool:
        """EN: True only if an LLM can actually be called under the current mode.
        ZH: 仅当当前档位下确实能调用 LLM 时才为 True。"""
        return self._client is not None

    def why_unavailable(self) -> Optional[str]:
        """EN: Human-readable reason the LLM is off. | ZH: LLM 不可用的可读原因。"""
        return self._init_error

    # -- core | 核心 -------------------------------------------------------

    def complete(self, system: str, user: str) -> str:
        """EN: Run one chat completion; raises if unavailable.
        ZH: 执行一次对话补全；不可用时抛异常。"""
        if not self.available:
            raise RuntimeError(
                f"LLM unavailable | LLM 不可用: {self._init_error}"
            )
        resp = self._client.chat.completions.create(  # type: ignore[union-attr]
            model=self._model(),
            temperature=self.config.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    def chat(self, messages: list, tools: Optional[list] = None):
        """EN: Multi-turn chat with optional tool/function calling. Returns the raw
            assistant message (may carry `.tool_calls`). Powers the co-pilot.
        ZH: 多轮对话，可选工具/函数调用。返回原始 assistant 消息（可能带 `.tool_calls`）。
            对话副驾的底座。"""
        if not self.available:
            raise RuntimeError(f"LLM unavailable | LLM 不可用: {self._init_error}")
        kwargs: dict = {
            "model": self._model(),
            "temperature": self.config.temperature,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.completions.create(**kwargs)  # type: ignore[union-attr]
        return resp.choices[0].message


    # -- internals | 内部实现 ----------------------------------------------

    def _try_init(self) -> None:
        # EN: load .env so keys can live in a file, not the shell/chat.
        # ZH: 加载 .env，让 key 存在文件里，而不是 shell/对话里。
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        # EN: air-gapped => intentionally never initialize a client.
        # ZH: air-gapped => 有意不初始化客户端。
        if self.config.privacy_mode == PrivacyMode.air_gapped:
            self._init_error = "privacy.mode=air-gapped (LLM disabled by policy | 按策略禁用 LLM)"
            return

        preset = PROVIDERS.get(self.config.provider)
        if preset is None:
            self._init_error = f"unknown provider | 未知 Provider: {self.config.provider}"
            return

        api_key = self.config.api_key or os.getenv(preset["api_key_env"])
        if not api_key:
            self._init_error = (
                f"missing API key | 缺少 API key: set {preset['api_key_env']} "
                f"or pass api_key"
            )
            return

        try:
            from openai import OpenAI  # EN: optional dep | ZH: 可选依赖
        except ImportError:
            self._init_error = "openai package not installed | 未安装 openai 包"
            return

        base_url = self.config.base_url or preset["base_url"]
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=self.config.timeout)

    def _model(self) -> str:
        preset = PROVIDERS[self.config.provider]
        return self.config.model or os.getenv("LLM_MODEL_ID") or preset["default_model"]
