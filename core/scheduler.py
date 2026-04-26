"""
core/scheduler.py — 定时自动总结任务调度
"""

import logging
import asyncio
import sys
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config import get_config
from core.wechat import get_global_reader
from ai.factory import create_provider_from_dict
from core.history import history_db

logger = logging.getLogger(__name__)

import tzlocal

# 全局调度器实例
_scheduler = BackgroundScheduler(timezone=tzlocal.get_localzone())
_job_id = "daily_summary_job"

def _run_async(coro, timeout=120):
    """
    在独立线程中运行 asyncio 协程，避免事件循环冲突。
    Windows 下强制使用 ProactorEventLoop。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(coro)

def scheduled_summarize_job():
    """定时执行的总结任务"""
    print("[scheduler] 定时任务触发了！", flush=True)
    logger.info("开始执行定时总结任务...")
    
    try:
        cfg = get_config()._data.get("scheduler", {})
        if not cfg.get("enabled"):
            logger.info("定时任务已禁用，跳过执行")
            return
            
        room_ids = cfg.get("room_ids", [])
        if not room_ids:
            logger.info("未配置需要自动总结的群聊，跳过执行")
            return
            
        mode = cfg.get("mode", "count")
        count = int(cfg.get("count", 200))
        time_range_type = cfg.get("time_range", "24h")
        
        # 获取 AI 配置
        ai_cfg = get_config().get_ai_config()
        if not ai_cfg.get("api_key") and ai_cfg.get("provider_type") != "ollama":
            logger.error("定时任务失败：AI 配置未完成（缺少 API Key）")
            return
            
        try:
            reader = get_global_reader()
        except Exception as exc:
            logger.error("定时任务失败：无法加载 WeChatReader (%s)", exc)
            return
            
        # 获取群名映射
        group_map = {}
        try:
            for g in reader.get_groups():
                group_map[g.room_id] = g.display_name
        except Exception:
            pass
            
        # 执行总结
        try:
            provider = create_provider_from_dict(ai_cfg)
            prompt_tmpl = ai_cfg.get("prompt_template", "tech")
            model_name = ai_cfg.get('model') or 'default'
            provider_model = f"{provider.provider_name} ({model_name})"
            timeout = int(ai_cfg.get("timeout", 120)) + 10
            
            # ---------------------------------------------------------
            # 1. 每次总结前，先同步最新的微信数据库
            # ---------------------------------------------------------
            try:
                from core.wechat import sync_database
                logger.info("定时任务：开始从微信原数据库同步最新消息...")
                success, msg = sync_database()
                if success:
                    logger.info("定时任务：数据库同步成功！")
                else:
                    logger.warning("定时任务：数据库同步提示异常：%s", msg)
            except Exception as exc:
                logger.error("定时任务：数据库同步发生错误：%s", exc)

            # ---------------------------------------------------------
            # 2. 依次对每个配置的群聊进行提取和总结
            # ---------------------------------------------------------
            for room_id in room_ids:
                group_name = group_map.get(room_id, room_id)
                logger.info("定时任务：正在处理群聊 [%s]", group_name)
                
                # 读取消息
                msgs = []
                if mode == "time":
                    now = datetime.now()
                    if time_range_type == "today":
                        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    elif time_range_type == "12h":
                        start_time = now - timedelta(hours=12)
                    elif time_range_type == "3d":
                        start_time = now - timedelta(days=3)
                    else: # 24h 默认
                        start_time = now - timedelta(hours=24)
                        
                    msgs = reader.get_messages(room_id, start_time=start_time)
                else:
                    msgs = reader.get_recent_messages(room_id, count=count)
                    
                if not msgs:
                    logger.info("定时任务：群聊 [%s] 无最新消息，跳过", group_name)
                    continue
                    
                # 生成时间范围
                t0 = msgs[0].create_time.strftime("%Y-%m-%d %H:%M")
                t1 = msgs[-1].create_time.strftime("%Y-%m-%d %H:%M")
                time_range = f"{t0} ~ {t1}"
                
                # 调用 AI
                try:
                    result = _run_async(
                        provider.summarize(msgs, group_name=group_name, time_range=time_range)
                    )
                    
                    # 存入历史
                    history_db.add_record(
                        room_id=room_id,
                        group_name=group_name,
                        time_range=time_range,
                        msg_count=len(msgs),
                        provider_model=provider_model,
                        prompt_template=prompt_tmpl,
                        content=result
                    )
                    logger.info("定时任务：群聊 [%s] 总结完成", group_name)
                except Exception as exc:
                    print(f"[scheduler] 群聊 [{group_name}] AI 总结出错: {exc}", flush=True)
                    logger.error("定时任务：群聊 [%s] AI 总结出错：%s", group_name, exc)
                    
        except Exception as exc:
            print(f"[scheduler] provider初始化或循环报错: {exc}", flush=True)
            logger.exception("定时任务执行AI循环报错")
            
    except Exception as exc:
        print(f"[scheduler] 定时任务最外层捕获到异常: {exc}", flush=True)
        logger.exception("定时任务最外层失败")

def start_scheduler():
    """启动调度器并初始化任务"""
    if not _scheduler.running:
        _scheduler.start()
        logger.info("APScheduler 调度器已启动")
    update_scheduler_job()

def update_scheduler_job():
    """根据配置更新定时任务"""
    cfg = get_config()._data.get("scheduler", {})
    
    # 移除旧任务
    if _scheduler.get_job(_job_id):
        _scheduler.remove_job(_job_id)
        
    enabled = cfg.get("enabled", False)
    time_str = cfg.get("time", "23:00")
    
    if enabled and time_str:
        try:
            hour, minute = map(int, time_str.split(':'))
            trigger = CronTrigger(hour=hour, minute=minute)
            _scheduler.add_job(
                scheduled_summarize_job, 
                trigger=trigger, 
                id=_job_id,
                replace_existing=True
            )
            logger.info("已设置定时自动总结任务：每天 %02d:%02d", hour, minute)
        except Exception as exc:
            logger.error("设置定时任务失败 (时间格式不正确?): %s", exc)
    else:
        logger.info("定时自动总结任务已关闭")
