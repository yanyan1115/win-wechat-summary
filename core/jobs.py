"""
core/jobs.py — 后台任务状态管理。

用于长耗时 AI 总结：HTTP 立即返回 job_id，前端轮询状态。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ai.base import (
    build_reduce_prompt,
    chunk_messages_by_token_budget,
    estimate_tokens,
    format_messages_for_ai,
    preprocess_messages,
)
from ai.factory import create_provider_from_dict
from core.history import history_db
from core.wechat import get_global_reader

logger = logging.getLogger(__name__)


@dataclass
class JobState:
    """单个后台任务状态。"""

    job_id: str
    status: str = "queued"
    progress: int = 0
    message: str = "任务已排队"
    error: str = ""
    result: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


_jobs: dict[str, JobState] = {}
_jobs_lock = threading.RLock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="summary-job")


def _run_async(coro: Any, timeout: int) -> Any:
    """在线程中运行异步 Provider 调用。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def _public_state(job: JobState) -> dict[str, Any]:
    data = asdict(job)
    data["created_at"] = datetime.fromtimestamp(job.created_at).strftime("%Y-%m-%d %H:%M:%S")
    data["updated_at"] = datetime.fromtimestamp(job.updated_at).strftime("%Y-%m-%d %H:%M:%S")
    return data


def _update(job_id: str, *, status: str | None = None, progress: int | None = None,
            message: str | None = None, error: str | None = None,
            result: dict[str, Any] | None = None, meta: dict[str, Any] | None = None) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = max(0, min(100, int(progress)))
        if message is not None:
            job.message = message
        if error is not None:
            job.error = error
        if result is not None:
            job.result = result
        if meta:
            job.meta.update(meta)
        job.updated_at = time.time()


def create_summary_job(payload: dict[str, Any], ai_cfg: dict[str, Any]) -> str:
    """创建群聊总结后台任务。"""
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = JobState(job_id=job_id)
    _executor.submit(_summary_worker, job_id, payload, ai_cfg)
    logger.info("已创建总结任务 job_id=%s", job_id)
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    """获取任务状态。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        return _public_state(job) if job else None


def cleanup_jobs(max_age_seconds: int = 3600) -> None:
    """清理过旧任务状态。"""
    cutoff = time.time() - max_age_seconds
    with _jobs_lock:
        old_ids = [
            job_id for job_id, job in _jobs.items()
            if job.updated_at < cutoff and job.status in {"done", "failed"}
        ]
        for job_id in old_ids:
            _jobs.pop(job_id, None)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _summarize_chunk(provider: Any, messages: list, group_name: str, time_range: str, timeout: int) -> str:
    if hasattr(provider, "summarize_chunk"):
        return _run_async(provider.summarize_chunk(messages, group_name=group_name, time_range=time_range), timeout)
    return _run_async(provider.summarize(messages, group_name=group_name, time_range=time_range), timeout)


def _reduce_summaries(provider: Any, partials: list[str], group_name: str,
                      time_range: str, original_count: int, timeout: int) -> str:
    if hasattr(provider, "reduce_summaries"):
        return _run_async(
            provider.reduce_summaries(
                partials,
                group_name=group_name,
                time_range=time_range,
                original_count=original_count,
            ),
            timeout,
        )
    prompt = build_reduce_prompt(partials, group_name, time_range, original_count)
    return _run_async(provider._call_stream(prompt), timeout)


def _summary_worker(job_id: str, payload: dict[str, Any], ai_cfg: dict[str, Any]) -> None:
    """执行总结任务。"""
    cleanup_jobs()
    try:
        _update(job_id, status="reading", progress=8, message="正在读取群聊消息")
        room_id = (payload.get("room_id") or "").strip()
        if not room_id:
            raise ValueError("缺少必填参数 room_id")

        mode = payload.get("mode", "count")
        count = int(payload.get("count", 200))
        start = _parse_dt(payload.get("start_time"))
        end = _parse_dt(payload.get("end_time"))

        reader = get_global_reader()
        if mode == "time":
            if not start or not end:
                raise ValueError("mode=time 时 start_time 和 end_time 为必填")
            msgs = reader.get_messages(room_id, start_time=start, end_time=end)
        else:
            msgs = reader.get_recent_messages(room_id, count=count)
        if not msgs:
            raise ValueError("该群在指定范围内没有消息")

        group_name = room_id
        try:
            groups = reader.get_groups()
            group = next((g for g in groups if g.room_id == room_id), None)
            if group:
                group_name = group.display_name
        except Exception as exc:
            logger.warning("任务 %s 获取群名称失败: %s", job_id, exc)

        t0 = msgs[0].create_time.strftime("%Y-%m-%d %H:%M")
        t1 = msgs[-1].create_time.strftime("%Y-%m-%d %H:%M")
        time_range = f"{t0} ~ {t1}"

        _update(job_id, status="preprocessing", progress=20, message="正在清洗低信息量消息")
        cleaned_msgs, stats = preprocess_messages(msgs)
        _update(
            job_id,
            progress=28,
            message=f"已清洗 {stats.original_count} 条消息，保留 {stats.cleaned_count} 条",
            meta={
                "original_count": stats.original_count,
                "cleaned_count": stats.cleaned_count,
                "dropped_count": stats.dropped_count,
                "estimated_tokens": stats.estimated_tokens,
            },
        )
        if not cleaned_msgs:
            result = "这段时间主要是低信息量消息，没有需要重点关注的内容。"
            _save_success(job_id, room_id, group_name, time_range, msgs, ai_cfg, "本地清洗", result)
            return

        _update(job_id, status="calling_ai", progress=35, message="正在初始化 AI Provider")
        provider = create_provider_from_dict(ai_cfg)
        timeout = int(ai_cfg.get("timeout", 120)) + 10
        budget = provider.get_input_token_budget(getattr(provider, "_model", ai_cfg.get("model", "")))
        chunk_budget = max(2_000, int(budget * 0.75))
        chunks = chunk_messages_by_token_budget(cleaned_msgs, chunk_budget)
        meta = {
            "input_budget": budget,
            "chunk_count": len(chunks),
            "estimated_tokens": estimate_tokens(format_messages_for_ai(cleaned_msgs)),
        }
        _update(job_id, meta=meta)

        if len(chunks) <= 1:
            _update(job_id, progress=50, message=f"正在请求 AI 总结（约 {stats.estimated_tokens} tokens）")
            result = _summarize_chunk(provider, cleaned_msgs, group_name, time_range, timeout)
        else:
            partials: list[str] = []
            total = len(chunks)
            for idx, chunk in enumerate(chunks, start=1):
                progress = 35 + int(45 * idx / max(1, total))
                _update(job_id, progress=progress, message=f"正在分块总结 {idx}/{total}")
                partials.append(
                    _summarize_chunk(
                        provider,
                        chunk,
                        group_name,
                        f"{time_range}（分段 {idx}/{total}）",
                        timeout,
                    )
                )
            _update(job_id, progress=85, message="正在合并分段摘要")
            result = _reduce_summaries(provider, partials, group_name, time_range, len(msgs), timeout)

        _save_success(job_id, room_id, group_name, time_range, msgs, ai_cfg, provider.provider_name, result)

    except Exception as exc:
        logger.exception("总结任务失败 job_id=%s", job_id)
        _update(job_id, status="failed", progress=100, message="任务失败", error=str(exc))


def _save_success(job_id: str, room_id: str, group_name: str, time_range: str,
                  msgs: list, ai_cfg: dict[str, Any], provider_name: str, result: str) -> None:
    _update(job_id, status="saving", progress=92, message="正在保存总结历史")
    prompt_tmpl = ai_cfg.get("prompt_template", "tech")
    model_name = ai_cfg.get("model") or "default"
    provider_model = f"{provider_name} ({model_name})"
    history_db.add_record(
        room_id=room_id,
        group_name=group_name,
        time_range=time_range,
        msg_count=len(msgs),
        provider_model=provider_model,
        prompt_template=prompt_tmpl,
        content=result,
    )
    if msgs:
        last_msg_time = msgs[-1].create_time.strftime("%Y-%m-%dT%H:%M")
        history_db.set_bookmark(room_id, last_msg_time)
    _update(
        job_id,
        status="done",
        progress=100,
        message="总结生成完成",
        result={
            "result": result,
            "msg_count": len(msgs),
            "group_name": group_name,
            "time_range": time_range,
            "provider": provider_name,
        },
    )
