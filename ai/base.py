"""
ai/base.py — AI Provider 抽象基类 + 统一 Prompt 模板

设计原则：
  - 所有 Provider 必须继承 AIProvider，实现 summarize 和 query 两个方法
  - Prompt 模板统一定义在本文件，各 Provider 直接调用，不得自行维护
  - 消息格式化逻辑也集中在此处，保证各端输出一致
"""

import logging
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
你是一个技术社群的群聊消息总结助手。你的任务是帮助没时间盯群的成员快速了解今天群里发生了什么有价值的内容。

## 群聊基本信息
- 群名称：{group_name}
- 消息时间段：{time_range}
- 消息总条数：{msg_count}

## 总结原则

1. **宁可详细，不要笼统**：每个有价值的话题都要展开说清楚，不要一笔带过
2. **保留具体信息**：教程名称、项目名称、工具名称、网址链接、关键参数、操作步骤等必须原样保留
3. **区分价值等级**：硬核技术分享 > 实用工具/资源 > 问题答疑 > 一般讨论 > 闲聊
4. **过滤噪音**：纯闲聊、表情包接龙、重复的"收到/谢谢"等直接跳过

## 输出格式

请按以下分类输出，每个分类下按时间顺序排列。没有内容的分类直接跳过。

### 📚 技术教程与知识分享
> 大佬们分享的教程、技术文章、硬核知识

对每个分享，写清楚：
- **谁分享的**（群昵称）
- **分享了什么**（标题/主题）
- **核心内容概要**（3~5 句话说清楚要点，不是一句话带过）
- **相关链接**（如果消息里有链接，原样保留）

### 🔧 实用工具与资源
> APP推荐、网站推荐、开源项目、插件工具等

对每个推荐，写清楚：
- **谁推荐的**
- **工具/资源名称**
- **是什么、能干什么**（一两句话说明）
- **链接或获取方式**

### 🤖 AI 使用心得与技巧
> 和 Claude/ChatGPT 等 AI 交流的心得、Prompt 技巧、使用场景分享

对每条心得，写清楚：
- **谁分享的**
- **具体心得/技巧是什么**（要写出可操作的细节）

### ❓ 问题与解答
> 群友提问 + 大佬解答

对每组问答，写清楚：
- **谁问了什么问题**
- **谁回答的，答案是什么**（写出具体解决方案，不是"有人回答了"）

### 💬 重要讨论
> 多人参与的有深度的讨论话题

对每个讨论，写清楚：
- **讨论的主题是什么**
- **主要观点有哪些**（列出不同人的观点）
- **有没有结论**

### 📌 待办与公告
> 群公告、活动通知、需要大家关注的事项

---

## 注意事项
- 如果某个话题特别有价值（比如详细的技术教程），宁可多写几段也不要压缩
- 链接、代码片段、命令行指令等原样保留，不要改写
- 如果消息里有图片/文件分享但你看不到内容，注明"[分享了图片/文件]"
- 用中文输出

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
        try:
            lines.append(msg.to_text_for_ai())
        except Exception as exc:
            logger.debug("格式化消息失败，跳过: %s", exc)
    return "\n".join(lines)


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
