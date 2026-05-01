"""
ai/claude_provider.py — Claude (Anthropic) AI Provider

使用 anthropic SDK (>=0.30) 的异步接口实现总结和问答功能。
支持流式输出（stream=True）以避免大消息体超时。
"""

import asyncio
import logging
from typing import Optional

import anthropic

from ai.base import (
    AIProvider,
    AIProviderConfigError,
    AIProviderError,
    AIProviderQuotaError,
    AIProviderTimeoutError,
    ProviderConfig,
    build_query_prompt,
    build_reduce_prompt,
    build_summary_prompt,
    chunk_messages_by_token_budget,
    estimate_tokens,
    preprocess_messages,
)

logger = logging.getLogger(__name__)

# Claude 各模型的上下文窗口（Token 数），超出时给出警告
_MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-opus-4-5":              200_000,
    "claude-sonnet-4-5":            200_000,
    "claude-3-7-sonnet-20250219":   200_000,
    "claude-3-5-sonnet-20241022":   200_000,
    "claude-3-5-haiku-20241022":    200_000,
    "claude-3-opus-20240229":       200_000,
    "claude-3-haiku-20240307":      200_000,
}
_DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


class ClaudeProvider(AIProvider):
    """
    Claude AI Provider。

    使用 anthropic.AsyncAnthropic 异步客户端，
    通过流式接口拼接完整回复，避免长消息超时。
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

        if not config.api_key:
            raise AIProviderConfigError(
                "Claude 需要 Anthropic API Key，请在设置中填写",
                provider="claude",
            )

        self._model = config.model or _DEFAULT_MODEL
        # 异步客户端（每次调用时复用）
        self._client = anthropic.AsyncAnthropic(
            api_key=config.api_key,
            timeout=float(config.timeout),
        )
        logger.info("ClaudeProvider 初始化完成，model=%s", self._model)

    @property
    def provider_name(self) -> str:
        return f"Claude ({self._model})"

    async def _call_stream(self, user_prompt: str) -> str:
        """
        通过流式接口调用 Claude，拼接完整回复文本。

        Args:
            user_prompt: 用户侧的完整 prompt（已含消息记录）。

        Returns:
            模型生成的完整文本。

        Raises:
            AIProviderError: API 调用失败。
        """
        async def _do_stream() -> str:
            full_text = []
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                system=self.system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                async for chunk in stream.text_stream:
                    full_text.append(chunk)
            return "".join(full_text).strip()

        try:
            result = await asyncio.wait_for(
                _do_stream(), timeout=self._config.timeout
            )
            logger.debug(
                "Claude 调用成功，输出 %d 字符（model=%s）",
                len(result), self._model,
            )
            return result

        except anthropic.AuthenticationError as exc:
            raise AIProviderConfigError(
                "Claude API Key 无效或已过期，请检查设置",
                provider="claude",
                raw_error=exc,
            ) from exc

        except anthropic.RateLimitError as exc:
            raise AIProviderQuotaError(
                "Claude API 请求频率超限，请稍后重试",
                provider="claude",
                status_code=429,
                raw_error=exc,
            ) from exc

        except anthropic.APIStatusError as exc:
            status = exc.status_code
            if status == 529:
                raise AIProviderError(
                    "Claude 服务过载（529），请稍后重试",
                    provider="claude",
                    status_code=status,
                    raw_error=exc,
                ) from exc
            raise AIProviderError(
                f"Claude API 错误（HTTP {status}）：{exc.message}",
                provider="claude",
                status_code=status,
                raw_error=exc,
            ) from exc

        except anthropic.APITimeoutError as exc:
            raise AIProviderTimeoutError(
                f"Claude 请求超时（超过 {self._config.timeout}s），"
                "建议减少消息条数或增大超时设置",
                provider="claude",
                raw_error=exc,
            ) from exc

        except anthropic.APIConnectionError as exc:
            raise AIProviderError(
                "无法连接到 Claude API，请检查网络或代理设置",
                provider="claude",
                raw_error=exc,
            ) from exc

        except Exception as exc:
            logger.exception("Claude 调用遇到未知异常")
            raise AIProviderError(
                f"Claude 调用失败：{exc}",
                provider="claude",
                raw_error=exc,
            ) from exc

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
            time_range: 时间范围描述，如 "2024-01-01 ~ 2024-01-07"。

        Returns:
            Markdown 格式的总结文本。
        """
        if not messages:
            return "（消息列表为空，无法生成总结）"

        cleaned_messages, stats = preprocess_messages(messages)
        if not cleaned_messages:
            logger.info("Claude summarize：清洗后无有效消息，跳过 AI 调用")
            return "这段时间主要是低信息量消息，没有需要重点关注的内容。"

        logger.info(
            "Claude summarize：群=%s，原始=%d，清洗后=%d，估算tokens=%d，model=%s",
            group_name, stats.original_count, stats.cleaned_count,
            stats.estimated_tokens, self._model,
        )
        budget = self.get_input_token_budget(self._model)
        if stats.estimated_tokens <= budget:
            prompt = build_summary_prompt(
                cleaned_messages, group_name, time_range,
                template_name=self._config.prompt_template,
                custom_prompt=self._config.custom_prompt
            )
            return await self._call_stream(prompt)

        chunk_budget = max(2_000, int(budget * 0.75))
        chunks = chunk_messages_by_token_budget(cleaned_messages, chunk_budget)
        logger.info(
            "Claude summarize 进入 Map-Reduce：群=%s，分块=%d，chunk_budget=%d",
            group_name, len(chunks), chunk_budget,
        )
        partials: list[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            chunk_prompt = build_summary_prompt(
                chunk,
                group_name,
                f"{time_range}（分段 {idx}/{len(chunks)}）",
                template_name=self._config.prompt_template,
                custom_prompt=self._config.custom_prompt,
            )
            partials.append(await self._call_stream(chunk_prompt))

        reduce_prompt = build_reduce_prompt(
            partials,
            group_name=group_name,
            time_range=time_range,
            original_count=len(messages),
        )
        if estimate_tokens(reduce_prompt) > budget:
            logger.warning("Claude Map-Reduce 汇总 Prompt 仍偏大，将截断分段摘要")
            partials = [p[:4000] for p in partials]
            reduce_prompt = build_reduce_prompt(
                partials,
                group_name=group_name,
                time_range=time_range,
                original_count=len(messages),
            )
        return await self._call_stream(reduce_prompt)

    async def summarize_chunk(
        self,
        messages: list,
        group_name: str = "",
        time_range: str = "",
    ) -> str:
        """为任务队列提供单块总结入口。"""
        prompt = build_summary_prompt(
            messages, group_name, time_range,
            template_name=self._config.prompt_template,
            custom_prompt=self._config.custom_prompt
        )
        return await self._call_stream(prompt)

    async def reduce_summaries(
        self,
        partial_summaries: list[str],
        group_name: str = "",
        time_range: str = "",
        original_count: int = 0,
    ) -> str:
        """为任务队列提供 Map-Reduce 汇总入口。"""
        prompt = build_reduce_prompt(
            partial_summaries,
            group_name=group_name,
            time_range=time_range,
            original_count=original_count,
        )
        return await self._call_stream(prompt)

    async def query(
        self,
        messages: list,
        question: str,
        group_name: str = "",
        time_range: str = "",
        keyword: str = "",
    ) -> str:
        """
        基于消息列表回答用户问题。

        Args:
            messages:   WeChatMessage 列表（已按需筛选）。
            question:   用户问题。
            group_name: 群名称。
            time_range: 时间范围描述。
            keyword:    检索关键词（有预筛选时传入）。

        Returns:
            AI 生成的回答文本。
        """
        if not messages:
            return "（消息列表为空，无法回答问题）"
        if not question or not question.strip():
            raise ValueError("问题不能为空")

        logger.info(
            "Claude query：群=%s，消息数=%d，问题=%r",
            group_name, len(messages), question[:50],
        )
        prompt = build_query_prompt(
            messages, question, group_name, time_range, keyword
        )
        return await self._call_stream(prompt)

    async def health_check(self) -> bool:
        """快速检查 Claude API 连通性（非流式，30s 超时）"""
        try:
            msg = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=10,
                    messages=[{"role": "user", "content": "回复ok"}],
                ),
                timeout=30,
            )
            return bool(msg.content)
        except Exception as exc:
            logger.warning("ClaudeProvider health_check 失败: %s", exc)
            return False
