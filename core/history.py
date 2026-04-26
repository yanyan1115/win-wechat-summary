"""
core/history.py — 总结历史存储模块

负责将生成的群聊总结保存到本地 SQLite 数据库中，并提供查询、删除等功能。
数据库默认保存在 ~/.wechat-summary/history.db
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

DB_DIR = Path.home() / ".wechat-summary"
DB_PATH = DB_DIR / "history.db"

class SummaryHistory:
    """管理总结历史的 SQLite 数据库"""
    
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()
        
    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接，设置自动转换 row 为 dict"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化数据库表结构"""
        query = """
        CREATE TABLE IF NOT EXISTS summary_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            group_name TEXT NOT NULL,
            time_range TEXT NOT NULL,
            msg_count INTEGER NOT NULL,
            provider_model TEXT NOT NULL,
            prompt_template TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
        try:
            with self._get_conn() as conn:
                conn.execute(query)
                # 创建索引以便按时间或群名查询
                conn.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON summary_history(created_at DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_group_name ON summary_history(group_name)")
            logger.info("历史数据库初始化成功: %s", self.db_path)
        except Exception as exc:
            logger.error("初始化历史数据库失败: %s", exc)

    def add_record(self, 
                   room_id: str, 
                   group_name: str, 
                   time_range: str, 
                   msg_count: int, 
                   provider_model: str, 
                   prompt_template: str, 
                   content: str) -> Optional[int]:
        """
        添加一条总结记录
        
        Returns:
            新记录的 id，如果失败返回 None
        """
        query = """
        INSERT INTO summary_history 
        (room_id, group_name, time_range, msg_count, provider_model, prompt_template, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(query, (
                    room_id, group_name, time_range, msg_count, 
                    provider_model, prompt_template, content, now
                ))
                return cursor.lastrowid
        except Exception as exc:
            logger.error("添加历史记录失败: %s", exc)
            return None

    def get_records(self, search_query: str = "", limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        获取历史记录列表
        
        Args:
            search_query: 按群名模糊搜索
            limit: 最大返回条数
            offset: 偏移量
            
        Returns:
            字典形式的记录列表
        """
        query = "SELECT * FROM summary_history"
        params: list[Any] = []
        
        if search_query:
            query += " WHERE group_name LIKE ?"
            params.append(f"%{search_query}%")
            
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except Exception as exc:
            logger.error("查询历史记录失败: %s", exc)
            return []

    def delete_record(self, record_id: int) -> bool:
        """
        删除单条历史记录
        
        Returns:
            是否删除成功
        """
        query = "DELETE FROM summary_history WHERE id = ?"
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(query, (record_id,))
                return cursor.rowcount > 0
        except Exception as exc:
            logger.error("删除历史记录失败: %s", exc)
            return False

# 提供一个全局实例以便使用
history_db = SummaryHistory()
