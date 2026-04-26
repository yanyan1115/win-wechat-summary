"""
config.py — 全局配置管理

配置文件路径：~/.wechat-summary/config.json
首次运行无需配置即可进入界面（使用默认值）。
API Key 当前明文存储，Phase 2 改用 Windows DPAPI 加密。
"""

import json
import logging
import ctypes
import base64
from ctypes import wintypes
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Windows DPAPI 加密函数 ────────────────────────

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

def encrypt_dpapi(data: str) -> str:
    if not data: return data
    try:
        data_bytes = data.encode('utf-8')
        blob_in = DATA_BLOB(len(data_bytes), ctypes.cast(data_bytes, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return "DPAPI:" + base64.b64encode(encrypted).decode('utf-8')
    except Exception as exc:
        logger.warning("DPAPI 加密失败: %s", exc)
    return data  # fallback

def decrypt_dpapi(b64_data: str) -> str:
    if not b64_data or not b64_data.startswith("DPAPI:"): 
        return b64_data
    try:
        encrypted = base64.b64decode(b64_data[6:])
        blob_in = DATA_BLOB(len(encrypted), ctypes.cast(encrypted, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return decrypted.decode('utf-8')
    except Exception as exc:
        logger.warning("DPAPI 解密失败: %s", exc)
    return b64_data  # fallback


CONFIG_DIR  = Path.home() / ".wechat-summary"
CONFIG_FILE = CONFIG_DIR / "config.json"

# 默认配置
DEFAULTS: dict[str, Any] = {
    "ai": {
        "provider_type": "deepseek",
        "api_key":       "",
        "model":         "",       # 留空 → factory 使用各模型默认值
        "timeout":       120,
        "max_tokens":    4096,
        "temperature":   0.3,
        "prompt_template": "tech", # 默认使用技术群模板
        "custom_prompt": "",       # 自定义模板内容
    },
    "wechat": {
        "conf_path": "",           # 留空 → 自动找 wxdump_work/conf_auto.json
    },
    "scheduler": {
        "enabled": False,
        "time": "23:00",
        "mode": "count",         # 'count' 或 'time'
        "count": 200,
        "time_range": "24h",     # '12h', '24h', 'today', '3d'
        "room_ids": [],
    },
    "app": {
        "host":  "127.0.0.1",
        "port":  5000,
        "debug": False,
    },
}


class Config:
    """配置管理器（单例）"""

    def __init__(self) -> None:
        self._data: dict = {}
        self._load()

    # ── 读写 ─────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载配置"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info("配置加载成功: %s", CONFIG_FILE)
            except Exception as exc:
                logger.warning("配置加载失败，使用默认值: %s", exc)
                self._data = {}
        else:
            logger.info("配置文件不存在，使用默认值: %s", CONFIG_FILE)
            self._data = {}

    def save(self) -> None:
        """持久化配置到磁盘"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        logger.info("配置保存成功: %s", CONFIG_FILE)

    # ── 通用 get/set ─────────────────────────────

    def get(self, *keys: str, default: Any = None) -> Any:
        """
        按层级路径读取配置，找不到时依次查默认值。
        例：config.get('ai', 'provider_type')
        """
        node: Any = self._data
        fallback: Any = DEFAULTS
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
                fallback = fallback.get(key) if isinstance(fallback, dict) else None
            elif isinstance(fallback, dict) and key in fallback:
                node = fallback[key]
                fallback = fallback[key] if isinstance(fallback.get(key), dict) else None
            else:
                return default
        return node

    def set(self, *keys_and_value: Any) -> None:
        """
        按层级路径写入配置（不自动保存，需手动调用 save()）。
        例：config.set('ai', 'api_key', 'sk-xxx')
        """
        *keys, value = keys_and_value
        node = self._data
        for key in keys[:-1]:
            if key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value

    # ── AI 配置快捷方式 ───────────────────────────

    def get_ai_config(self) -> dict:
        """返回用于创建 AIProvider 的配置字典"""
        ai = self._data.get("ai", {})
        d  = DEFAULTS["ai"]
        return {
            "provider_type": ai.get("provider_type", d["provider_type"]),
            "api_key":       decrypt_dpapi(ai.get("api_key", d["api_key"])),
            "model":         ai.get("model",         d["model"]),
            "timeout":       int(ai.get("timeout",   d["timeout"])),
            "max_tokens":    int(ai.get("max_tokens",d["max_tokens"])),
            "temperature":   float(ai.get("temperature", d["temperature"])),
            "prompt_template": ai.get("prompt_template", d["prompt_template"]),
            "custom_prompt": ai.get("custom_prompt", d["custom_prompt"]),
        }

    def set_ai_config(self, cfg: dict) -> None:
        """批量更新 AI 配置并保存"""
        if "ai" not in self._data:
            self._data["ai"] = {}
        allowed = {"provider_type", "api_key", "model", "timeout", "max_tokens", "temperature", "prompt_template", "custom_prompt"}
        for k, v in cfg.items():
            if k in allowed:
                # 拒绝保存被前端脱敏的 API Key
                if k == "api_key" and isinstance(v, str) and "****" in v:
                    continue
                # 新的 API Key 进行加密
                if k == "api_key" and v:
                    v = encrypt_dpapi(v)
                self._data["ai"][k] = v
        self.save()

    # ── WeChatReader 配置 ─────────────────────────

    def get_wechat_conf_path(self) -> str:
        """
        获取 PyWxDump conf_auto.json 的路径。
        未配置时返回空字符串，让 create_reader_from_config() 自动查找。
        """
        return self._data.get("wechat", {}).get("conf_path", "")

    # ── 序列化（脱敏） ────────────────────────────

    def to_dict_safe(self) -> dict:
        """返回配置字典，API Key 部分脱敏（用于前端展示）"""
        import copy
        safe = copy.deepcopy(self._data)
        
        # 确保包含 scheduler 默认值
        if "scheduler" not in safe:
            safe["scheduler"] = copy.deepcopy(DEFAULTS["scheduler"])
            
        key = decrypt_dpapi(safe.get("ai", {}).get("api_key", ""))
        if key and len(key) > 8:
            safe.setdefault("ai", {})["api_key"] = key[:4] + "****" + key[-4:]
        elif key:
            safe.setdefault("ai", {})["api_key"] = key
        else:
            safe.setdefault("ai", {})["api_key"] = ""
            
        return safe

    def to_dict_full(self) -> dict:
        """返回完整配置字典（含明文 Key，仅供内部使用）"""
        import copy
        return copy.deepcopy(self._data)


# ── 全局单例 ─────────────────────────────────────

_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = Config()
    return _config
