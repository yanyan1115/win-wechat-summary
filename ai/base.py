"""
ai/base.py — AI Provider 抽象基类 + 统一 Prompt 模板

设计原则：
  - 所有 Provider 必须继承 AIProvider，实现 summarize 和 query 两个方法
  - Prompt 模板统一定义在本文件，各 Provider 直接调用，不得自行维护
  - 消息格式化逻辑也集中在此处，保证各端输出一致
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Prompt 模板
# ──────────────────────────────────────────────

# 通用系统角色设定
_SYSTEM_ROLE = "你是一个专业的群聊消息分析助手，擅长从大量聊天记录中提炼关键信息，输出结构清晰的中文摘要。"

# ── 总结 Prompt 模板集合 ──────────────────────

PROMPT_TEMPLATES = {
    "tech": """\
你是一个技术社群的群聊消息总结助手。

## 群聊基本信息
- 群名称：{group_name}
- 消息时间段：{time_range}
- 消息总条数：{msg_count}

## 你的目标

帮没时间看群消息的人快速了解：今天群里有哪些值得关注的内容。
他们看完你的总结后，应该能直接获得有用的信息，而不是还要回去翻聊天记录。

## 核心原则

1. **一个话题只出现一次**：不要把同一件事拆到多个分类里重复写。选最合适的那个分类放进去。
2. **写信息本身，不要写聊天过程**：
   - ❌ "群友A问了教程在哪，群友B告知是盏老师做的，并分享了链接"
   - ✅ "盏老师制作了 wda-mcp 教程，可以让 Claude 远程控制 iPhone。详细文档：[链接]"
3. **写能直接用的干货**：具体的操作步骤、参数、命令、链接、结论，都要保留。
4. **人名要准确**：仔细看每条消息前面的发送者名字，不要搞混。如果无法确定是谁说的，就写"有群友提到"。

## 输出格式

请按话题分类输出。每个话题只归入一个最合适的分类。没有内容的分类直接跳过。

---

### 📚 技术分享与教程

> 有人分享了技术知识、教程、文章、实操经验

每个话题写成这样：
**[话题标题]**（分享者：xxx）
- 简要说明是什么（1~2句话）
- 关键内容 / 核心步骤 / 要点（用几句话把干货写清楚，能直接看懂不用翻原文）
- 相关链接（如果有的话原样保留）
- 实用技巧或注意事项（如果群友补充了有用的经验也写上）

### 🔧 工具、APP 与资源推荐

> 有人推荐了好用的工具、APP、网站、开源项目、插件等
> ⚠️ 如果这个工具已经在"技术分享"里作为教程的一部分写过了，这里不要重复

每个推荐写成这样：
**[工具名称]**（推荐者：xxx）
- 是什么、能干什么（1~2句话）
- 链接或获取方式

### 💰 福利与羊毛

> 优惠信息、免费资源、限时活动、省钱技巧等

每条写清楚：
**[标题]**（分享者：xxx）
- 具体内容和参与方式
- 限时/限量条件（如果有的话）
- 链接

### 🤖 AI 使用心得

> 关于 Claude、ChatGPT 等 AI 的使用技巧、Prompt 技巧、踩坑经验

每条写清楚：
**[心得主题]**（分享者：xxx）
- 具体怎么做 / 有什么效果（要写出可操作的细节）

### ❓ 问题与解决方案

> 有人提了问题并且得到了有用的回答
> ⚠️ 如果这个问答已经包含在上面某个话题里了，不要重复

每组写成这样：
**问题：xxx**（提问者：xxx）
**解决方案：** 直接写结论和操作方法（不要写"某某回答了"这种过程描述）

### 💬 值得关注的讨论

> 多人参与的、有深度或有趣的讨论
> 只收录有实质内容的讨论，纯闲聊跳过

**[讨论主题]**
- 主要观点（直接写观点内容，不需要逐一列出谁说了什么）
- 结论（如果有的话）

### ✅ 待办事项

> 明确有人需要执行、报名、提交、确认或跟进的事项

**[待办标题]**（负责人：xxx / 未指定）
- 需要做什么
- 截止时间或条件（如果有）

---

## 过滤规则

直接跳过以下内容：
- 纯闲聊、打招呼、表情包
- "收到""谢谢""哈哈"等无信息量的回复
- 重复的内容（同一条消息被多人转发）
- 撤回的消息
- 入群/退群通知

## 输出要求

- 用中文
- 链接、代码、命令原样保留
- 如果消息中有图片/文件你看不到内容，标注 [图片] 或 [文件]
- 如果整段时间没有什么有价值的内容，直接说"这段时间主要是闲聊，没有需要关注的技术内容"，不要硬凑

## 以下是需要总结的群聊消息：

{messages}
""",
    "general": """\
你是一个专业的群聊消息总结助手。请对下方群聊消息进行深度总结。

【群聊基本信息】
群名称：{group_name}
消息时间段：{time_range}
消息总条数：{msg_count}

【总结要求】
1. 按话题分类整理，每个话题单独成节，格式：
   ## 话题：<话题名>
   - 要点 1
   - 要点 2
2. 提取关键信息和结论，忽略无意义的闲聊、纯表情、打招呼
3. 标注重要的 @提及：格式 【@提及】某人被提及做...
4. 提取待办事项（如"记得""需要""你去""帮忙""提醒"等）：格式 【待办】...
5. 如有重要决策或共识，在末尾单独列出：
   ## 重要决策 & 共识
6. 控制总结篇幅：话题 ≤ 8 个，每话题要点 ≤ 5 条
7. 全程使用中文输出

【消息记录】
{messages}
"""
}

def get_prompt_template(template_name: str, custom_prompt: str = "") -> str:
    """根据配置获取对应的 Prompt 模板内容"""
    if template_name == "custom" and custom_prompt.strip():
        return custom_prompt
    return PROMPT_TEMPLATES.get(template_name, PROMPT_TEMPLATES["tech"])

# ── 关键词问答 Prompt ──────────────────────────

QUERY_PROMPT_TEMPLATE = """\
你是一个专业的群聊信息检索助手。以下是群聊「{group_name}」在 {time_range} 内，\
包含关键词「{keyword}」的消息记录。

请根据这些消息，回答用户的问题。
要求：
1. 只基于聊天记录中的信息作答，不要编造
2. 引用具体消息时，注明时间和发送者
3. 若消息不足以回答问题，明确说明"消息记录中未找到相关信息"
4. 使用中文回答，格式清晰

【相关消息记录】
{messages}

【用户问题】
{question}
"""

# ── 自由问答 Prompt（无关键词预筛选时使用）──────

FREE_QUERY_PROMPT_TEMPLATE = """\
你是一个专业的群聊信息检索助手。以下是群聊「{group_name}」的消息记录（{time_range}）。

请根据这些消息回答用户的问题。
要求：
1. 只基于聊天记录中的信息作答，不要编造
2. 引用具体消息时，注明时间和发送者
3. 若消息不足以回答问题，明确说明"消息记录中未找到相关信息"
4. 使用中文回答，格式清晰

【消息记录】
{messages}

【用户问题】
{question}
"""


# ──────────────────────────────────────────────
# 消息格式化工具
# ──────────────────────────────────────────────

LOW_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^.{0,20}拍了拍.{0,20}$"),
    re.compile(r"^.{0,20}撤回了一条消息.{0,20}$"),
    re.compile(r"^你撤回了一条消息$"),
    re.compile(r"^(.{1,20})加入了群聊$"),
    re.compile(r"^(.{1,20})退出了群聊$"),
    re.compile(r"^(.{1,20})邀请(.{1,30})加入了群聊$"),
)
LOW_VALUE_TEXTS: set[str] = {
    "收到", "好的", "好", "嗯", "嗯嗯", "是的", "对", "可以", "ok", "OK",
    "谢谢", "感谢", "辛苦了", "哈哈", "哈哈哈", "hh", "hhh", "赞", "牛",
    "了解", "明白", "已阅", "1", "+1",
}
PLACEHOLDER_LIMITS: dict[str, int] = {
    "[图片]": 3,
    "[表情]": 2,
    "[语音]": 2,
    "[视频]": 2,
    "[位置]": 2,
}
DEFAULT_INPUT_TOKEN_BUDGET = 24_000


@dataclass
class MessagePreprocessStats:
    """消息预处理统计信息。"""

    original_count: int
    cleaned_count: int
    dropped_count: int
    estimated_tokens: int
    chunk_count: int = 1


def estimate_tokens(text: str) -> int:
    """
    粗略估算输入 token 数。

    中文按字符偏保守估算，ASCII 连续片段按约 4 字符 1 token 估算。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, non_ascii_chars + (ascii_chars + 3) // 4)


def context_limit_for_model(provider_type: str, model: str) -> int:
    """根据 provider/model 返回保守上下文窗口。"""
    name = (model or "").lower()
    provider = (provider_type or "").lower()

    if "claude" in provider or "claude" in name:
        return 200_000
    if "gpt-4o" in name or "gpt-4.1" in name or "gpt-5" in name:
        return 128_000
    if "deepseek" in provider or "deepseek" in name:
        return 64_000
    if "qwen" in provider or "qwen" in name:
        return 32_000
    if "ollama" in provider:
        return 8_000
    return 32_000


def input_budget_for_model(provider_type: str, model: str, max_output_tokens: int) -> int:
    """给模型输入预留安全预算，避免顶满上下文。"""
    context_limit = context_limit_for_model(provider_type, model)
    reserved = max(max_output_tokens + 2_000, int(context_limit * 0.15))
    return max(4_000, min(DEFAULT_INPUT_TOKEN_BUDGET, context_limit - reserved))


def _is_mostly_symbol(text: str) -> bool:
    if not text:
        return True
    useful = sum(1 for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return useful == 0 or (len(text) <= 8 and useful <= 1)


def _is_low_value_text(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    if not compact:
        return True
    if compact in LOW_VALUE_TEXTS:
        return True
    if len(compact) <= 2 and not any(ch in compact for ch in ("?", "？", "!", "！")):
        return True
    if _is_mostly_symbol(compact):
        return True
    return any(pattern.search(compact) for pattern in LOW_VALUE_PATTERNS)


def preprocess_messages(messages: list) -> tuple[list, MessagePreprocessStats]:
    """
    清洗低信息量消息，减少 token 浪费。

    保留 URL、代码、问句、金额、时间等高价值信息；过滤拍一拍、撤回、纯表情、重复占位符。
    """
    cleaned: list = []
    seen_text: set[str] = set()
    placeholder_counts: dict[str, int] = {}

    for msg in messages:
        try:
            raw_text = msg.to_text_for_ai()
        except Exception as exc:
            logger.warning("格式化消息失败，跳过: %s", exc)
            continue

        body = raw_text.split(": ", 1)[1] if ": " in raw_text else raw_text
        body = body.strip()

        if body.startswith("[系统]") and _is_low_value_text(body[4:]):
            continue

        if body in PLACEHOLDER_LIMITS:
            used = placeholder_counts.get(body, 0)
            if used >= PLACEHOLDER_LIMITS[body]:
                continue
            placeholder_counts[body] = used + 1
        elif _is_low_value_text(body):
            continue

        dedupe_key = re.sub(r"\s+", "", body)
        if dedupe_key and dedupe_key in seen_text and len(dedupe_key) > 20:
            continue
        if dedupe_key:
            seen_text.add(dedupe_key)
        cleaned.append(msg)

    msg_text = "\n".join(_safe_message_line(m) for m in cleaned)
    stats = MessagePreprocessStats(
        original_count=len(messages),
        cleaned_count=len(cleaned),
        dropped_count=max(0, len(messages) - len(cleaned)),
        estimated_tokens=estimate_tokens(msg_text),
    )
    return cleaned, stats


def _safe_message_line(msg: object) -> str:
    try:
        return msg.to_text_for_ai()
    except Exception as exc:
        logger.warning("格式化消息失败，跳过: %s", exc)
        return ""


def format_messages_for_ai(messages: list) -> str:
    """
    将 WeChatMessage 列表转成适合投喂给 AI 的纯文本。

    格式：
        [HH:MM] 发送者: 消息内容
        [系统] 系统消息内容

    Args:
        messages: WeChatMessage 对象列表（来自 core/wechat.py）

    Returns:
        多行字符串，每条消息一行。
    """
    lines: list[str] = []
    for msg in messages:
        line = _safe_message_line(msg)
        if line:
            lines.append(line)
    return "\n".join(lines)


def chunk_messages_by_token_budget(messages: list, token_budget: int) -> list[list]:
    """按 token 预算把消息切成多个时间顺序分块。"""
    if not messages:
        return []

    chunks: list[list] = []
    current: list = []
    current_tokens = 0

    for msg in messages:
        line = _safe_message_line(msg)
        msg_tokens = max(1, estimate_tokens(line) + 2)
        if current and current_tokens + msg_tokens > token_budget:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(msg)
        current_tokens += msg_tokens

    if current:
        chunks.append(current)
    return chunks


def build_reduce_prompt(
    partial_summaries: list[str],
    group_name: str,
    time_range: str = "",
    original_count: int = 0,
) -> str:
    """构建 Map-Reduce 最终汇总 Prompt。"""
    joined = "\n\n---\n\n".join(
        f"【分段 {idx}】\n{summary.strip()}"
        for idx, summary in enumerate(partial_summaries, start=1)
        if summary.strip()
    )
    return f"""\
你是一个专业的群聊消息分析助手。下面是同一个群聊按时间分段得到的初步摘要。

请把这些分段摘要合并成一份最终总结，去重、合并相同话题，保留重要链接、命令、决策和待办事项。

【群聊信息】
- 群名称：{group_name or "未知群"}
- 消息时间段：{time_range or "不限"}
- 原始消息条数：{original_count}
- 分段摘要数：{len(partial_summaries)}

【分段摘要】
{joined}
"""


def build_summary_prompt(
    messages: list,
    group_name: str,
    time_range: str = "",
    template_name: str = "tech",
    custom_prompt: str = "",
) -> str:
    """
    构建群聊总结 Prompt。

    Args:
        messages:   WeChatMessage 列表。
        group_name: 群名称，用于上下文说明。
        time_range: 时间范围描述，如"2024-01-01 至 2024-01-07"。
        template_name: 使用的 Prompt 模板名（'tech', 'general', 'custom'）
        custom_prompt: 自定义 Prompt 内容（template_name 为 'custom' 时生效）

    Returns:
        完整的 user prompt 字符串。
    """
    msg_text = format_messages_for_ai(messages)
    template = get_prompt_template(template_name, custom_prompt)
    
    return template.format(
        group_name=group_name,
        time_range=time_range or "不限",
        msg_count=len(messages),
        messages=msg_text,
    )


def build_query_prompt(
    messages: list,
    question: str,
    group_name: str = "",
    time_range: str = "",
    keyword: str = "",
) -> str:
    """
    构建基于消息的问答 Prompt。

    Args:
        messages:   WeChatMessage 列表（已按关键词筛选或按时间段取出）。
        question:   用户问题。
        group_name: 群名称。
        time_range: 时间范围描述。
        keyword:    若有关键词筛选，传入关键词（用于 prompt 上下文），否则留空。

    Returns:
        完整的 user prompt 字符串。
    """
    msg_text = format_messages_for_ai(messages)
    if keyword:
        return QUERY_PROMPT_TEMPLATE.format(
            group_name=group_name or "未知群",
            time_range=time_range or "不限",
            keyword=keyword,
            messages=msg_text,
            question=question,
        )
    else:
        return FREE_QUERY_PROMPT_TEMPLATE.format(
            group_name=group_name or "未知群",
            time_range=time_range or "不限",
            messages=msg_text,
            question=question,
        )


# ──────────────────────────────────────────────
# Provider 配置数据类
# ──────────────────────────────────────────────

@dataclass
class ProviderConfig:
    """
    创建 AI Provider 所需的配置项。
    由 config.py 读取并填充，再传入工厂函数。
    """
    provider_type: str        # "claude" | "openai" | "deepseek" | "qwen" | "ollama"
    api_key: str = ""         # API Key（Ollama 留空）
    model: str = ""           # 模型名称（留空则使用 Provider 内置默认值）
    base_url: str = ""        # 自定义 API 端点（OpenAI 兼容模式时使用）
    timeout: int = 120        # 请求超时（秒）
    max_tokens: int = 4096    # 最大输出 Token 数
    temperature: float = 0.3  # 温度（总结任务建议偏低，更稳定）
    prompt_template: str = "tech" # 总结 Prompt 模板名称
    custom_prompt: str = ""   # 自定义 Prompt 内容


# ──────────────────────────────────────────────
# 抽象基类
# ──────────────────────────────────────────────

class AIProvider(ABC):
    """
    AI Provider 抽象基类。

    所有具体 Provider（Claude、OpenAI、Ollama…）必须继承本类并实现：
      - summarize()  生成群聊总结
      - query()      基于消息回答用户问题

    子类可覆盖 system_prompt 属性来自定义系统提示词，
    但 Prompt 模板本身不得在子类中重新定义，统一使用 base.py 中的模板。
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    def system_prompt(self) -> str:
        """系统角色提示词，子类可覆盖"""
        return _SYSTEM_ROLE

    @property
    def provider_name(self) -> str:
        """Provider 展示名称"""
        return self._config.provider_type

    @property
    def config(self) -> ProviderConfig:
        """Provider 配置（只读使用）。"""
        return self._config

    def get_input_token_budget(self, model: str = "") -> int:
        """获取当前 Provider 的输入 token 预算。"""
        return input_budget_for_model(
            self._config.provider_type,
            model or self._config.model,
            self._config.max_tokens,
        )

    @abstractmethod
    async def summarize(
        self,
        messages: list,
        group_name: str = "",
        time_range: str = "",
    ) -> str:
        """
        生成群聊消息总结。

        Args:
            messages:   WeChatMessage 列表。
            group_name: 群名称，注入到 Prompt 上下文。
            time_range: 时间范围描述，注入到 Prompt 上下文。

        Returns:
            AI 生成的 Markdown 格式总结字符串。

        Raises:
            AIProviderError: API 调用失败时抛出。
        """
        ...

    @abstractmethod
    async def query(
        self,
        messages: list,
        question: str,
        group_name: str = "",
        time_range: str = "",
        keyword: str = "",
    ) -> str:
        """
        基于消息列表回答用户的自然语言问题。

        Args:
            messages:   WeChatMessage 列表（已按需筛选）。
            question:   用户问题。
            group_name: 群名称。
            time_range: 时间范围描述。
            keyword:    关键词（有筛选时传入，用于 Prompt 上下文）。

        Returns:
            AI 生成的回答字符串。

        Raises:
            AIProviderError: API 调用失败时抛出。
        """
        ...

    async def health_check(self) -> bool:
        """
        检查 Provider 是否可用（发送一条最简单的测试消息）。
        子类可覆盖以实现更精确的检查。
        默认实现：调用一次极短的 summarize，成功则返回 True。

        Returns:
            True = 可用，False = 不可用。
        """
        try:
            # 构造一个极简的假消息测试连通性
            class _FakeMsg:
                def to_text_for_ai(self) -> str:
                    return "[00:00] 测试用户: 这是一条连通性测试消息"
            await self.summarize([_FakeMsg()], group_name="测试群")
            return True
        except Exception as exc:
            self._logger.warning("health_check 失败: %s", exc)
            return False


# ──────────────────────────────────────────────
# 自定义异常
# ──────────────────────────────────────────────

class AIProviderError(Exception):
    """AI Provider 调用失败的统一异常类"""

    def __init__(
        self,
        message: str,
        provider: str = "",
        status_code: Optional[int] = None,
        raw_error: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.raw_error = raw_error

    def __str__(self) -> str:
        parts = [self.args[0]]
        if self.provider:
            parts.append(f"[{self.provider}]")
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        return " ".join(parts)


class AIProviderConfigError(AIProviderError):
    """配置错误（如 API Key 未填写）"""
    pass


class AIProviderQuotaError(AIProviderError):
    """配额不足 / 余额不足"""
    pass


class AIProviderTimeoutError(AIProviderError):
    """请求超时"""
    pass
