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
from core.jobs import create_summary_job, get_job
from core.wechat import get_global_reader
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
    import sqlite3 as _sqlite3
    from core.wechat import sync_database, reset_global_reader, _get_merge_path
    success, msg = sync_database()
    if success:
        reset_global_reader()
        latest_time = None
        try:
            merge_path = _get_merge_path()
            uri = f"file:{merge_path}?mode=ro"
            conn = _sqlite3.connect(uri, uri=True, timeout=30)
            try:
                conn.execute("PRAGMA query_only = ON")
                row = conn.execute("SELECT MAX(CreateTime) FROM MSG").fetchone()
            finally:
                conn.close()
            if row and row[0]:
                latest_time = datetime.fromtimestamp(row[0]).strftime("%H:%M")
        except Exception as _e:
            logger.warning("获取最新消息时间失败: %s", _e)
        return _ok(message="同步成功", latest_time=latest_time)
    else:
        return _err(f"同步失败: {msg}", 500)

@bp.post("/api/summarize")
def api_summarize():
    """
    创建群聊消息总结后台任务，立即返回 job_id。

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

    mode = body.get("mode", "count")
    if mode == "time" and (not body.get("start_time") or not body.get("end_time")):
        return _err("mode=time 时 start_time 和 end_time 为必填")

    # 确定 AI Provider 配置
    override = body.get("provider") or {}
    ai_cfg   = get_config().get_ai_config()
    ai_cfg.update({k: v for k, v in override.items() if v})

    if not ai_cfg.get("api_key") and ai_cfg.get("provider_type") != "ollama":
        return _err("AI API Key 未配置，请先在设置中填写", 400)

    try:
        job_id = create_summary_job(body, ai_cfg)
        return _ok(job_id=job_id, status="queued", message="总结任务已创建")
    except Exception as exc:
        logger.exception("创建总结任务失败")
        return _err(f"创建总结任务失败：{exc}", 500)


@bp.get("/api/jobs/<job_id>")
def api_job_status(job_id: str):
    """查询后台任务状态。"""
    state = get_job(job_id)
    if state is None:
        return _err("任务不存在或已过期", 404)
    return _ok(state)


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


# ── 路由：阅读书签 ────────────────────────────────

@bp.get("/api/bookmark/<room_id>")
def api_bookmark_get(room_id: str):
    """获取指定群的书签（上次总结截止时间）"""
    bookmark = history_db.get_bookmark(room_id)
    return _ok(bookmark=bookmark)


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


# ── 路由：初始化向导 ──────────────────────────────

@bp.get("/api/setup/status")
def api_setup_status():
    """检查是否已完成初始化（conf_auto.json 是否存在）"""
    from core.wechat import _app_root
    conf_path = _app_root() / "wxdump_work" / "conf_auto.json"
    return _ok(initialized=conf_path.exists())


@bp.get("/api/setup/detect")
def api_setup_detect():
    """
    自动从运行中的微信进程读取账号信息。
    微信必须处于登录状态。
    """
    try:
        import ctypes
        import psutil
        from pywxdump import get_wx_info, WX_OFFS

        # 先检查微信进程是否存在（不需要管理员权限）
        wx_pids = [p.pid for p in psutil.process_iter(["name"]) if p.info["name"] == "WeChat.exe"]
        if not wx_pids:
            return _err("未检测到微信进程，请确保微信已启动并登录", 404)

        results = get_wx_info(WX_OFFS)
        # 过滤掉 key 为空的项
        valid = [r for r in results if r.get("key")]
        if not valid:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
            if not is_admin:
                return _err("检测到微信进程但无法读取密钥，请右键以管理员身份运行本程序", 403)
            # 有管理员权限但仍读不到 key：版本不支持
            detected_versions = list({r.get("version") for r in results if r.get("version")})
            ver_hint = f"（检测到版本：{', '.join(detected_versions)}）" if detected_versions else ""
            return _err(
                f"可能是微信版本不受支持，当前支持到 3.9.12.55，请检查你的微信版本{ver_hint}",
                404,
            )
        # 脱敏手机号和邮箱再返回给前端
        safe = []
        for r in valid:
            safe.append({
                "wxid":     r.get("wxid", ""),
                "nickname": r.get("nickname", ""),
                "account":  r.get("account", ""),
                "version":  r.get("version", ""),
                "wx_dir":   r.get("wx_dir", ""),
                "key":      r.get("key", ""),   # 需要保存，但不显示
            })
        return _ok(safe)
    except Exception as exc:
        logger.exception("自动检测微信信息失败")
        return _err(f"检测失败：{exc}", 500)


@bp.post("/api/setup/save")
def api_setup_save():
    """
    保存检测到的微信信息为 conf_auto.json，完成初始化。
    请求体：{ wxid, nickname, wx_dir, key }
    """
    body = request.get_json(force=True, silent=True) or {}
    wxid   = (body.get("wxid") or "").strip()
    wx_dir = (body.get("wx_dir") or "").strip()
    key    = (body.get("key") or "").strip()

    if not wxid or not wx_dir or not key:
        return _err("缺少必要字段 wxid / wx_dir / key")

    try:
        from core.wechat import _app_root, reset_global_reader
        import json
        from pathlib import Path

        root = _app_root()
        wxdump_dir = root / "wxdump_work"
        wxdump_dir.mkdir(parents=True, exist_ok=True)

        merge_path = str(wxdump_dir / wxid / "merge_all.db")
        (wxdump_dir / wxid).mkdir(parents=True, exist_ok=True)

        conf = {
            "auto_setting": {"last": wxid},
            wxid: {
                "key":        key,
                "wx_path":    wx_dir,
                "merge_path": merge_path,
                "my_wxid":    wxid,
                "db_config": {
                    "key":  key,
                    "type": "sqlite",
                    "path": merge_path,
                }
            }
        }
        conf_path = wxdump_dir / "conf_auto.json"
        with open(conf_path, "w", encoding="utf-8") as f:
            json.dump(conf, f, ensure_ascii=False, indent=4)

        # 重置 reader 以便用新配置重新初始化
        reset_global_reader()

        logger.info("初始化完成：conf_auto.json 已写入 %s", conf_path)
        return _ok(message="初始化成功，正在同步数据库...")
    except Exception as exc:
        logger.exception("保存初始化配置失败")
        return _err(f"保存失败：{exc}", 500)


