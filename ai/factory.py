"""
ai/factory.py — AI Provider 工厂函数

根据 ProviderConfig 中的 provider_type 创建对应的 Provider 实例。
支持动态导入：只有实际使用到的 Provider 才会被 import，
避免因为某个 SDK 未安装而导致整个应用崩溃。

支持的 provider_type：
  "claude"   → ClaudeProvider   (anthropic SDK)
  "openai"   → OpenAIProvider   (openai SDK, 默认端点)
  "deepseek" → OpenAIProvider   (openai SDK, DeepSeek 端点)
  "qwen"     → OpenAIProvider   (openai SDK, DashScope 端点)
  "ollama"   → OllamaProvider   (HTTP localhost)
"""

import importlib
import logging
from typing import Optional

from ai.base import AIProvider, AIProviderConfigError, ProviderConfig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# OpenAI 兼容模型的预设端点和默认模型名
# ──────────────────────────────────────────────

_OPENAI_PRESETS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "",                                                      # 使用 SDK 默认
        "default_model": "gpt-4o",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-flash",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
}

# 各 provider_type → (模块路径, 类名)
_PROVIDER_MAP: dict[str, tuple[str, str]] = {
    "claude":   ("ai.claude_provider",  "ClaudeProvider"),
    "openai":   ("ai.openai_provider",  "OpenAIProvider"),
    "deepseek": ("ai.openai_provider",  "OpenAIProvider"),
    "qwen":     ("ai.openai_provider",  "OpenAIProvider"),
    "ollama":   ("ai.ollama_provider",  "OllamaProvider"),
}


# ──────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────

def create_provider(config: ProviderConfig) -> AIProvider:
    """
    根据 ProviderConfig 创建对应的 AI Provider 实例。

    Args:
        config: 包含 provider_type、api_key、model 等字段的配置对象。

    Returns:
        实现了 AIProvider 接口的具体 Provider 实例。

    Raises:
        AIProviderConfigError: provider_type 不支持，或 API Key 缺失。
        ImportError:           对应 SDK 未安装。
    """
    ptype = config.provider_type.lower().strip()

    if ptype not in _PROVIDER_MAP:
        supported = ", ".join(_PROVIDER_MAP.keys())
        raise AIProviderConfigError(
            f"不支持的 provider_type: '{ptype}'，支持的类型: {supported}",
            provider=ptype,
        )

    # ── OpenAI 兼容模型：补全 base_url 和默认 model ──
    if ptype in _OPENAI_PRESETS:
        preset = _OPENAI_PRESETS[ptype]
        if not config.base_url:
            config.base_url = preset["base_url"]
        if not config.model:
            config.model = preset["default_model"]

    # ── Claude 默认 model ──
    if ptype == "claude" and not config.model:
        config.model = "claude-3-5-sonnet-20241022"

    # ── Ollama 默认 model ──
    if ptype == "ollama" and not config.model:
        config.model = "qwen2.5:7b"

    # ── 校验 API Key（Ollama 不需要）──
    if ptype != "ollama" and not config.api_key:
        raise AIProviderConfigError(
            f"provider '{ptype}' 需要 API Key，但 config.api_key 为空",
            provider=ptype,
        )

    # ── 动态导入 Provider 类 ──
    module_path, class_name = _PROVIDER_MAP[ptype]
    try:
        module = importlib.import_module(module_path)
        provider_cls = getattr(module, class_name)
    except ImportError as exc:
        # 给出友好的安装提示
        _sdk_hint = {
            "claude":   "anthropic",
            "openai":   "openai",
            "deepseek": "openai",
            "qwen":     "openai",
            "ollama":   "requests",
        }
        sdk = _sdk_hint.get(ptype, "对应 SDK")
        raise ImportError(
            f"无法导入 {module_path}，请先安装依赖：pip install {sdk}\n原始错误: {exc}"
        ) from exc
    except AttributeError as exc:
        raise AIProviderConfigError(
            f"模块 {module_path} 中未找到类 {class_name}: {exc}",
            provider=ptype,
        ) from exc

    provider = provider_cls(config)
    logger.info(
        "创建 AI Provider 成功：type=%s, model=%s, base_url=%s",
        ptype, config.model, config.base_url or "（SDK 默认）",
    )
    return provider


def create_provider_from_dict(raw: dict) -> AIProvider:
    """
    从字典创建 Provider（方便 config.py / Flask routes 调用）。

    示例：
        provider = create_provider_from_dict({
            "provider_type": "deepseek",
            "api_key": "sk-xxx",
            "model": "deepseek-v4-flash",       # 可省略，使用预设
        })

    Args:
        raw: 包含配置项的字典，键名与 ProviderConfig 字段一致。

    Returns:
        AIProvider 实例。
    """
    config = ProviderConfig(
        provider_type=raw.get("provider_type", ""),
        api_key=raw.get("api_key", ""),
        model=raw.get("model", ""),
        base_url=raw.get("base_url", ""),
        timeout=int(raw.get("timeout", 120)),
        max_tokens=int(raw.get("max_tokens", 4096)),
        temperature=float(raw.get("temperature", 0.3)),
    )
    return create_provider(config)


# ──────────────────────────────────────────────
# 便捷构造函数（用于快速测试）
# ──────────────────────────────────────────────

def make_claude(api_key: str, model: str = "") -> AIProvider:
    """快速创建 Claude Provider"""
    return create_provider(ProviderConfig(
        provider_type="claude", api_key=api_key, model=model
    ))


def make_deepseek(api_key: str, model: str = "") -> AIProvider:
    """快速创建 DeepSeek Provider"""
    return create_provider(ProviderConfig(
        provider_type="deepseek", api_key=api_key, model=model
    ))


def make_qwen(api_key: str, model: str = "") -> AIProvider:
    """快速创建通义千问 Provider"""
    return create_provider(ProviderConfig(
        provider_type="qwen", api_key=api_key, model=model
    ))


def make_openai(api_key: str, model: str = "") -> AIProvider:
    """快速创建 ChatGPT Provider"""
    return create_provider(ProviderConfig(
        provider_type="openai", api_key=api_key, model=model
    ))


def make_ollama(model: str = "qwen2.5:7b", base_url: str = "") -> AIProvider:
    """快速创建 Ollama Provider（本地模型，无需 API Key）"""
    return create_provider(ProviderConfig(
        provider_type="ollama",
        api_key="",
        model=model,
        base_url=base_url or "http://localhost:11434",
    ))


# ──────────────────────────────────────────────
# 支持的 Provider 列表（供前端展示）
# ──────────────────────────────────────────────

def list_supported_providers() -> list[dict]:
    """
    返回所有支持的 Provider 信息列表，供前端的设置页面使用。

    Returns:
        list of dict，每项包含：
            id          provider_type 字符串
            name        展示名称
            need_key    是否需要 API Key
            default_model  默认模型名
            base_url    API 端点（空字符串表示使用 SDK 默认）
    """
    return [
        {
            "id": "claude",
            "name": "Claude (Anthropic)",
            "need_key": True,
            "default_model": "claude-3-5-sonnet-20241022",
            "base_url": "https://api.anthropic.com",
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "need_key": True,
            "default_model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com/v1",
        },
        {
            "id": "qwen",
            "name": "通义千问 (阿里云)",
            "need_key": True,
            "default_model": "qwen-plus",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
        {
            "id": "openai",
            "name": "ChatGPT (OpenAI)",
            "need_key": True,
            "default_model": "gpt-4o",
            "base_url": "",
        },
        {
            "id": "ollama",
            "name": "Ollama (本地模型)",
            "need_key": False,
            "default_model": "qwen2.5:7b",
            "base_url": "http://localhost:11434",
        },
    ]
