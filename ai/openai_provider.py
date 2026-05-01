"""
ai/openai_provider.py — OpenAI 兼容 AI Provider

同时支持以下三个模型（通过 base_url 区分）：
  - ChatGPT    (openai SDK 默认端点)
  - DeepSeek   (base_url = https://api.deepseek.com/v1)
  - 通义千问    (base_url = https://dashscope.aliyuncs.com/compatible-mode/v1)

使用 openai SDK (>=2.0) 的异步接口。
"""

import asyncio
import logging

import openai

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

# 各端点对应的默认模型
_DEFAULT_MODELS: dict[str, str] = {
    "":                                                          "gpt-4o",
    "https://api.deepseek.com/v1":                              "deepseek-v4-flash",
    "https://dashscope.aliyuncs.com/compatible-mode/v1":        "qwen-plus",
}


class OpenAIProvider(AIProvider):
    """
    OpenAI 兼容 AI Provider。

    DeepSeek 和通义千问与 OpenAI API 格式完全兼容，
    只需改 base_url 和 api_key，其余逻辑完全共用。
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

        if not config.api_key:
            raise AIProviderConfigError(
                f"OpenAI 兼容 Provider 需要 API Key（base_url={config.base_url or 'OpenAI 默认'}）",
                provider=config.provider_type,
            )

        # 确定 base_url（空字符串 = 使用 SDK 默认值）
        base_url: str | None = config.base_url if config.base_url else None

        # 确定默认 model
        self._model = config.model or _DEFAULT_MODELS.get(config.base_url or "", "gpt-4o")

        # 异步客户端
        client_kwargs: dict = dict(
            api_key=config.api_key,
            timeout=float(config.timeout),
        )
        if base_url:
            client_kwargs["base_url"] = base_url

        self._client = openai.AsyncOpenAI(**client_kwargs)

        logger.info(
            "OpenAIProvider 初始化完成，provider=%s, model=%s, base_url=%s",
            config.provider_type, self._model, base_url or "（SDK 默认）",
        )

    @property
    def provider_name(self) -> str:
        label = {
            "openai":   "ChatGPT",
            "deepseek": "DeepSeek",
            "qwen":     "通义千问",
        }.get(self._config.provider_type, "OpenAI 兼容")
        return f"{label} ({self._model})"

    async def _call_stream(self, user_prompt: str) -> str:
        """
        通过流式接口调用 OpenAI 兼容 API，拼接完整回复文本。

        Args:
            user_prompt: 完整的 user 侧 prompt。

        Returns:
            模型生成的完整文本。

        Raises:
            AIProviderError: 各类 API 错误。
        """
        async def _do_request() -> str:
            full_text: list[str] = []
            stream = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                stream=True,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    full_text.append(delta.content)
            return "".join(full_text).strip()

        try:
            result = await asyncio.wait_for(
                _do_request(), timeout=self._config.timeout
            )
            logger.debug(
                "OpenAI 兼容调用成功，输出 %d 字符（model=%s）",
                len(result), self._model,
            )
            return result

        except openai.AuthenticationError as exc:
            raise AIProviderConfigError(
                f"{self.provider_name} API Key 无效或已过期，请检查设置",
                provider=self._config.provider_type,
                status_code=401,
                raw_error=exc,
            ) from exc

        except openai.RateLimitError as exc:
            raise AIProviderQuotaError(
                f"{self.provider_name} 请求频率超限或余额不足，请稍后重试",
                provider=self._config.provider_type,
                status_code=429,
                raw_error=exc,
            ) from exc

        except openai.BadRequestError as exc:
            raise AIProviderError(
                f"{self.provider_name} 请求参数错误：{exc.message}",
                provider=self._config.provider_type,
                status_code=400,
                raw_error=exc,
            ) from exc

        except openai.APITimeoutError as exc:
            raise AIProviderTimeoutError(
                f"{self.provider_name} 请求超时（超过 {self._config.timeout}s），"
                "建议减少消息条数或增大超时设置",
                provider=self._config.provider_type,
                raw_error=exc,
            ) from exc

        except openai.APIConnectionError as exc:
            raise AIProviderError(
                f"无法连接到 {self.provider_name} API，请检查网络或代理设置",
                provider=self._config.provider_type,
                raw_error=exc,
            ) from exc

        except openai.APIStatusError as exc:
            raise AIProviderError(
                f"{self.provider_name} API 错误（HTTP {exc.status_code}）：{exc.message}",
                provider=self._config.provider_type,
                status_code=exc.status_code,
                raw_error=exc,
            ) from exc

        except Exception as exc:
            logger.exception("OpenAI 兼容 Provider 调用遇到未知异常")
            raise AIProviderError(
                f"{self.provider_name} 调用失败：{exc}",
                provider=self._config.provider_type,
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
            group_name: 群名称。
            time_range: 时间范围描述。

        Returns:
            Markdown 格式的总结文本。
        """
        if not messages:
            return "（消息列表为空，无法生成总结）"

        cleaned_messages, stats = preprocess_messages(messages)
        if not cleaned_messages:
            logger.info("OpenAI 兼容 summarize：清洗后无有效消息，跳过 AI 调用")
            return "这段时间主要是低信息量消息，没有需要重点关注的内容。"

        logger.info(
            "OpenAI 兼容 summarize：provider=%s，群=%s，原始=%d，清洗后=%d，估算tokens=%d",
            self._config.provider_type, group_name, stats.original_count,
            stats.cleaned_count, stats.estimated_tokens,
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
            "OpenAI 兼容 summarize 进入 Map-Reduce：provider=%s，群=%s，分块=%d，chunk_budget=%d",
            self._config.provider_type, group_name, len(chunks), chunk_budget,
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
            logger.warning("OpenAI 兼容 Map-Reduce 汇总 Prompt 仍偏大，将截断分段摘要")
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
            messages:   WeChatMessage 列表。
            question:   用户问题。
            group_name: 群名称。
            time_range: 时间范围描述。
            keyword:    检索关键词。

        Returns:
            AI 生成的回答文本。
        """
        if not messages:
            return "（消息列表为空，无法回答问题）"
        if not question or not question.strip():
            raise ValueError("问题不能为空")

        logger.info(
            "OpenAI 兼容 query：provider=%s，群=%s，消息数=%d，问题=%r",
            self._config.provider_type, group_name, len(messages), question[:50],
        )
        prompt = build_query_prompt(
            messages, question, group_name, time_range, keyword
        )
        return await self._call_stream(prompt)

    async def health_check(self) -> bool:
        """快速检查 API 连通性（非流式，30s 超时）"""
        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    max_tokens=5,
                    stream=False,
                    messages=[{"role": "user", "content": "回复ok"}],
                ),
                timeout=30,
            )
            return bool(resp.choices and resp.choices[0].message.content)
        except Exception as exc:
            logger.warning("OpenAIProvider health_check 失败: %s", exc)
            return False
