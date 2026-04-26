"""
web/routes.py — Flask API 路由层（Blueprint）

所有 API 路径统一前缀 /api/。
异步 AI 调用通过独立线程运行，避免阻塞 Flask 主线程。
"""

import asyncio
import concurrent.futures
import logging
import sys
from datetime import datetime
from typing import Any

from flask import Blueprint, jsonify, request, Response

from config import get_config
from ai.factory import create_provider_from_dict, list_supported_providers
from core.wechat import WeChatReader, get_global_reader
from core.history import history_db

logger = logging.getLogger(__name__)
bp = Blueprint("api", __name__)


# ── 异步桥接 ──────────────────────────────────────

def _run_async(coro: Any, timeout: int = 130) -> Any:
    """
    在独立线程中运行 asyncio 协程，避免与 Flask 事件循环冲突。
    Windows 下强制使用 ProactorEventLoop。
    """
    def _runner():
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_runner)
        return future.result(timeout=timeout)


# ── 工具函数 ──────────────────────────────────────

def _ok(data: Any = None, **kwargs) -> Response:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(kwargs)
    return jsonify(payload)


def _err(msg: str, code: int = 400) -> tuple[Response, int]:
    return jsonify({"ok": False, "error": msg}), code


def _parse_dt(s: str | None) -> datetime | None:
    """把前端 ISO 字符串解析为 datetime；格式不对则返回 None"""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ── 路由：状态 & 配置 ─────────────────────────────

@bp.get("/api/status")
def api_status():
    """检查 DB 连通性和 AI 配置状态"""
    db_ok = False
    db_error = ""
    try:
        get_global_reader()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    cfg = get_config().get_ai_config()
    ai_configured = bool(cfg.get("api_key") or cfg.get("provider_type") == "ollama")

    return _ok(
        db_ok=db_ok,
        db_error=db_error,
        ai_configured=ai_configured,
        provider_type=cfg.get("provider_type", ""),
        model=cfg.get("model", ""),
    )


@bp.get("/api/config")
def api_config_get():
    """返回当前配置（API Key 脱敏）"""
    return _ok(get_config().to_dict_safe())


@bp.post("/api/config")
def api_config_save():
    """保存 AI 配置"""
    body = request.get_json(force=True, silent=True) or {}
    ai_cfg = body.get("ai", {})
    if not ai_cfg:
        return _err("请求体中缺少 'ai' 字段")
    try:
        get_config().set_ai_config(ai_cfg)
        return _ok(message="配置保存成功")
    except Exception as exc:
        logger.exception("保存配置失败")
        return _err(f"保存失败：{exc}", 500)


@bp.get("/api/providers")
def api_providers():
    """返回支持的 AI Provider 列表"""
    return _ok(list_supported_providers())


# ── 路由：群聊数据 ────────────────────────────────

@bp.get("/api/groups")
def api_groups():
    """获取所有群聊列表"""
    try:
        reader = get_global_reader()
        groups = reader.get_groups()
        return _ok([
            {
                "room_id":      g.room_id,
                "name":         g.name,
                "remark":       g.remark,
                "display_name": g.display_name,
                "member_count": g.member_count,
                "announcement": g.announcement[:100] if g.announcement else "",
            }
            for g in groups
        ])
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except Exception as exc:
        logger.exception("获取群列表失败")
        return _err(f"获取群列表失败：{exc}", 500)


@bp.get("/api/groups/<room_id>/count")
def api_group_count(room_id: str):
    """统计群消息数（用于前端预估）"""
    start = _parse_dt(request.args.get("start_time"))
    end   = _parse_dt(request.args.get("end_time"))
    try:
        reader = get_global_reader()
        count  = reader.get_message_count(room_id, start, end)
        return _ok(count=count)
    except Exception as exc:
        return _err(str(exc), 500)


# ── 路由：AI 总结 ─────────────────────────────────

@bp.post("/api/sync")
def api_sync():
    """手动触发数据库同步"""
    from core.wechat import sync_database
    success, msg = sync_database()
    if success:
        return _ok(message="同步成功")
    else:
        return _err(f"同步失败: {msg}", 500)

@bp.post("/api/summarize")
def api_summarize():
    """
    生成群聊消息总结。

    请求体（JSON）：
      room_id     str   必填，群 ID
      mode        str   "time"（按时间段）或 "count"（按最近 N 条）
      start_time  str   mode=time 时使用，ISO 格式
      end_time    str   mode=time 时使用
      count       int   mode=count 时使用，默认 200
      provider    dict  可选，覆盖全局 AI 配置（含 provider_type / api_key / model）
    """
    body = request.get_json(force=True, silent=True) or {}

    room_id = (body.get("room_id") or "").strip()
    if not room_id:
        return _err("缺少必填参数 room_id")

    mode  = body.get("mode", "count")
    count = int(body.get("count", 200))
    start = _parse_dt(body.get("start_time"))
    end   = _parse_dt(body.get("end_time"))

    # 读取消息
    try:
        reader = get_global_reader()
        if mode == "time":
            if not start or not end:
                return _err("mode=time 时 start_time 和 end_time 为必填")
            msgs = reader.get_messages(room_id, start_time=start, end_time=end)
        else:
            msgs = reader.get_recent_messages(room_id, count=count)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except Exception as exc:
        logger.exception("读取消息失败")
        return _err(f"读取消息失败：{exc}", 500)

    if not msgs:
        return _err("该群在指定范围内没有消息", 404)

    # 获取群名
    group_name = room_id
    try:
        groups = reader.get_groups()
        g = next((g for g in groups if g.room_id == room_id), None)
        if g:
            group_name = g.display_name
    except Exception:
        pass

    # 时间范围描述
    if msgs:
        t0 = msgs[0].create_time.strftime("%Y-%m-%d %H:%M")
        t1 = msgs[-1].create_time.strftime("%Y-%m-%d %H:%M")
        time_range = f"{t0} ~ {t1}"
    else:
        time_range = ""

    # 确定 AI Provider 配置
    override = body.get("provider") or {}
    ai_cfg   = get_config().get_ai_config()
    ai_cfg.update({k: v for k, v in override.items() if v})

    if not ai_cfg.get("api_key") and ai_cfg.get("provider_type") != "ollama":
        return _err("AI API Key 未配置，请先在设置中填写", 400)

    # 调用 AI（在独立线程中运行异步代码）
    try:
        provider = create_provider_from_dict(ai_cfg)
        timeout  = int(ai_cfg.get("timeout", 120)) + 10

        result = _run_async(
            provider.summarize(msgs, group_name=group_name, time_range=time_range),
            timeout=timeout,
        )

        # 保存到历史记录
        prompt_tmpl = ai_cfg.get("prompt_template", "tech")
        model_name = ai_cfg.get('model') or 'default'
        provider_model = f"{provider.provider_name} ({model_name})"
        history_db.add_record(
            room_id=room_id,
            group_name=group_name,
            time_range=time_range,
            msg_count=len(msgs),
            provider_model=provider_model,
            prompt_template=prompt_tmpl,
            content=result
        )

        return _ok(
            result=result,
            msg_count=len(msgs),
            group_name=group_name,
            time_range=time_range,
            provider=provider.provider_name,
        )

    except concurrent.futures.TimeoutError:
        return _err("AI 请求超时，请减少消息条数或增大超时设置", 504)
    except Exception as exc:
        logger.exception("AI 总结失败")
        return _err(f"AI 总结失败：{exc}", 500)


# ── 路由：关键词搜索 ──────────────────────────────

@bp.post("/api/search")
def api_search():
    """
    关键词搜索群聊消息。

    请求体（JSON）：
      keyword     str        必填
      room_ids    list[str]  可选，限定群范围（空则搜全部）
      start_time  str        可选
      end_time    str        可选
      limit       int        最多返回条数，默认 100
      summarize   bool       是否同时生成 AI 归纳（默认 false）
      provider    dict       可选，覆盖 AI 配置
    """
    body = request.get_json(force=True, silent=True) or {}

    keyword  = (body.get("keyword") or "").strip()
    if not keyword:
        return _err("缺少必填参数 keyword")

    room_ids   = body.get("room_ids") or None
    start      = _parse_dt(body.get("start_time"))
    end        = _parse_dt(body.get("end_time"))
    limit      = int(body.get("limit", 100))
    do_summary = bool(body.get("summarize", False))

    try:
        reader = get_global_reader()
        msgs   = reader.search(keyword, room_ids=room_ids,
                               start_time=start, end_time=end, limit=limit)
    except RuntimeError as exc:
        return _err(str(exc), 503)
    except Exception as exc:
        logger.exception("搜索失败")
        return _err(f"搜索失败：{exc}", 500)

    # 格式化消息列表返回
    result_msgs = [
        {
            "room_id":    m.room_id,
            "sender":     m.sender_name or m.sender_id,
            "content":    m.content,
            "time":       m.create_time.strftime("%Y-%m-%d %H:%M"),
            "type_label": m.type_label,
        }
        for m in msgs
    ]

    # 可选：AI 归纳
    ai_summary = ""
    if do_summary and msgs:
        override = body.get("provider") or {}
        ai_cfg   = get_config().get_ai_config()
        ai_cfg.update({k: v for k, v in override.items() if v})

        if not ai_cfg.get("api_key") and ai_cfg.get("provider_type") != "ollama":
            return _err("AI API Key 未配置，请先在设置中填写", 400)

        try:
            provider = create_provider_from_dict(ai_cfg)
            ai_summary = _run_async(
                provider.query(msgs, question=f"请归纳和总结以下包含关键词「{keyword}」的消息",
                               keyword=keyword),
                timeout=60,
            )
        except Exception as exc:
            ai_summary = f"（AI 归纳失败：{exc}）"

    return _ok(
        messages=result_msgs,
        count=len(msgs),
        ai_summary=ai_summary,
        keyword=keyword,
    )


# ── 路由：总结历史 ────────────────────────────────

@bp.get("/api/history")
def api_history_list():
    """获取历史总结列表"""
    search = request.args.get("search", "").strip()
    limit = int(request.args.get("limit", 100))
    records = history_db.get_records(search_query=search, limit=limit)
    return _ok(records)


@bp.delete("/api/history/<int:record_id>")
def api_history_delete(record_id: int):
    """删除单条总结记录"""
    success = history_db.delete_record(record_id)
    if success:
        return _ok(message="删除成功")
    return _err("记录不存在或删除失败", 404)


# ── 路由：定时任务配置 ────────────────────────────

@bp.get("/api/scheduler/config")
def api_scheduler_config_get():
    """获取定时任务配置"""
    cfg = get_config()._data.get("scheduler", {})
    return _ok(cfg)

@bp.post("/api/scheduler/config")
def api_scheduler_config_save():
    """保存定时任务配置并应用生效"""
    body = request.get_json(force=True, silent=True) or {}
    try:
        from core.scheduler import update_scheduler_job
        cfg = get_config()._data.setdefault("scheduler", {})
        
        # 更新配置项
        if "enabled" in body: cfg["enabled"] = bool(body["enabled"])
        if "time" in body: cfg["time"] = body["time"]
        if "mode" in body: cfg["mode"] = body["mode"]
        if "count" in body: cfg["count"] = int(body["count"])
        if "time_range" in body: cfg["time_range"] = body["time_range"]
        if "room_ids" in body: cfg["room_ids"] = body["room_ids"]
        
        get_config().save()
        
        # 通知调度器更新
        update_scheduler_job()
        
        return _ok(message="定时任务设置已保存并生效")
    except Exception as exc:
        logger.exception("保存定时配置失败")
        return _err(f"保存失败：{exc}", 500)


