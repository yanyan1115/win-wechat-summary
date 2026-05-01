"""
core/wechat.py — 微信本地数据库只读读取模块

基于 PyWxDump 解密后的 merge_all.db，提供：
- WeChatGroup    群聊信息数据类
- WeChatMessage  消息数据类
- WeChatReader   核心读取类（获取群列表、读取消息、关键词搜索）

重要安全约束：
  所有数据库连接均使用 URI 模式 + ?mode=ro，严禁任何写操作。
"""

import json
import logging
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _app_root() -> Path:
    """
    返回应用根目录：
    - 打包为 exe 时：exe 文件所在目录（而非 _MEI 临时解压目录）
    - 直接运行 py 时：项目根目录（core/ 的上级）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent

logger = logging.getLogger(__name__)

_db_access_lock = threading.RLock()
_sync_lock = threading.Lock()


class DatabaseSyncError(RuntimeError):
    """微信数据库同步失败。"""


class _LockedConnection:
    """在连接关闭前持有全局数据库锁，避免同步替换与读取互相抢占。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> "_LockedConnection":
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return self._conn.__exit__(exc_type, exc, tb)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._conn.close()
        finally:
            self._closed = True
            self._lock.release()

# ──────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────

@dataclass
class WeChatGroup:
    """群聊基本信息"""
    room_id: str          # 群 ID，如 "12345678@chatroom"
    name: str             # 群名（NickName 或 Remark）
    remark: str           # 备注名（空则与 name 相同）
    member_count: int     # 成员数（可能不准确，来自 UserNameList）
    announcement: str     # 群公告（可能为空）

    @property
    def display_name(self) -> str:
        """优先展示备注名，其次群名，最后用 room_id"""
        return self.remark or self.name or self.room_id


@dataclass
class WeChatMessage:
    """单条聊天消息"""
    local_id: int           # 本地消息 ID
    msg_svr_id: int         # 服务端消息 ID
    room_id: str            # 所属群 ID
    sender_id: str          # 发送者 wxid（自己发的则为 my_wxid）
    sender_name: str        # 发送者昵称（尽力获取，可能为空）
    is_sender: bool         # True = 自己发的
    msg_type: int           # 消息类型（1=文字，3=图片，43=视频，49=卡片，…）
    content: str            # 消息内容（文字消息直接是正文，其他类型为 XML）
    create_time: datetime   # 消息发送时间
    display_content: str    # 微信内部显示文本（引用/撤回等场景）

    @property
    def is_text(self) -> bool:
        return self.msg_type == 1

    @property
    def type_label(self) -> str:
        _map = {
            1: "文字", 3: "图片", 34: "语音", 43: "视频",
            47: "表情", 48: "位置", 49: "卡片/链接",
            10000: "系统消息",
        }
        return _map.get(self.msg_type, f"未知({self.msg_type})")

    def to_text_for_ai(self) -> str:
        """
        转成适合投喂给 AI 的纯文本。
        非文字消息用 [类型] 占位，避免 XML 干扰 AI。
        """
        ts = self.create_time.strftime("%H:%M")
        name = self.sender_name or self.sender_id
        if self.is_text:
            body = self.content.strip()
        elif self.msg_type == 10000:
            # 系统消息：撤回、入群、退群等
            body = f"[系统]{self.display_content or self.content}"
        else:
            body = f"[{self.type_label}]"
        return f"[{ts}] {name}: {body}"


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────

def _parse_sender_from_bytes_extra(data: Optional[bytes]) -> str:
    """
    从 MSG.BytesExtra 字段中解析发送者 wxid。

    BytesExtra 是 Protobuf 编码。结构（已通过实测确认）：
      repeated field 3 (tag=0x1a, wire=2):
        field 1 (tag=0x0a, wire=2): string → wxid
    
    这里使用纯手工解析，避免引入 protobuf 依赖。
    返回空字符串表示解析失败。
    """
    if not data:
        return ""
    try:
        i = 0
        n = len(data)
        while i < n:
            # 读取 tag varint
            tag, i = _read_varint(data, i)
            if tag is None:
                break
            field_num = tag >> 3
            wire_type = tag & 0x7

            if wire_type == 0:
                # varint — 跳过
                _, i = _read_varint(data, i)
            elif wire_type == 2:
                # length-delimited
                length, i = _read_varint(data, i)
                if length is None:
                    break
                nested = data[i: i + length]
                i += length
                if field_num == 3:
                    # 解析嵌套结构，找 field 1 (wxid)
                    wxid = _parse_nested_wxid(nested)
                    if wxid:
                        return wxid
            elif wire_type == 1:
                i += 8   # 64-bit fixed
            elif wire_type == 5:
                i += 4   # 32-bit fixed
            else:
                break    # 未知 wire type，停止
    except Exception as exc:
        logger.debug("BytesExtra 解析异常: %s", exc)
    return ""


def _parse_nested_wxid(data: bytes) -> str:
    """解析 BytesExtra.field3 嵌套结构，返回 field1 字符串（wxid）"""
    i = 0
    n = len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        if tag is None:
            break
        field_num = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:
            _, i = _read_varint(data, i)
        elif wire_type == 2:
            length, i = _read_varint(data, i)
            if length is None:
                break
            val = data[i: i + length]
            i += length
            if field_num == 1:
                return val.decode("utf-8", errors="replace")
        elif wire_type == 1:
            i += 8
        elif wire_type == 5:
            i += 4
        else:
            break
    return ""


def _read_varint(data: bytes, pos: int) -> tuple[Optional[int], int]:
    """读取 protobuf varint，返回 (value, new_pos)"""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            return None, pos
    return None, pos


# ──────────────────────────────────────────────
# 主类
# ──────────────────────────────────────────────

class WeChatReader:
    """
    微信本地数据库只读读取器。

    使用前提：
      PyWxDump 已完成解密，生成了 merge_all.db。
      merge_all.db 路径由 conf_auto.json 中 merge_path 字段指定。

    线程安全：每次调用方法都重新建立连接（适合低频使用）。
    如需高并发，可改为连接池。
    """

    # 群聊消息 Type 过滤：默认只保留以下类型
    _USEFUL_MSG_TYPES: set[int] = {1, 3, 34, 43, 47, 48, 49, 10000}
    # Type=1:文字  3:图片  34:语音  43:视频  47:表情  48:位置  49:卡片/链接  10000:系统消息

    # ChatRoom.UserNameList 的分隔符
    _MEMBER_SEP = "\x1e"   # 有时是 \x1e（ASCII 30），有时是 ^G（\x07）
    _MEMBER_SEP2 = "^G"

    def __init__(self, db_path: str, my_wxid: str = ""):
        """
        初始化读取器。

        Args:
            db_path:  merge_all.db 的绝对路径。
            my_wxid:  当前登录账号的 wxid，用于标注"自己发送"的消息。
        """
        self._db_path = Path(db_path)
        self._my_wxid = my_wxid
        if not self._db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在: {db_path}")
        logger.info("WeChatReader 初始化成功，数据库: %s", db_path)

    # ── 内部工具 ──────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """建立只读数据库连接"""
        _db_access_lock.acquire()
        uri = f"file:{self._db_path}?mode=ro"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=30,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            return _LockedConnection(conn, _db_access_lock)  # type: ignore[return-value]
        except Exception:
            _db_access_lock.release()
            raise

    def _build_member_cache(
        self, conn: sqlite3.Connection
    ) -> dict[str, dict[str, str]]:
        """
        构建 {room_id: {wxid: display_name}} 的成员昵称缓存。
        数据来源：ChatRoom.UserNameList / DisplayNameList。
        """
        cache: dict[str, dict[str, str]] = {}
        cur = conn.cursor()
        try:
            cur.execute("SELECT ChatRoomName, UserNameList, DisplayNameList FROM ChatRoom")
            for row in cur.fetchall():
                def _b(v) -> str:
                    if v is None: return ""
                    if isinstance(v, bytes): return v.decode("utf-8", errors="ignore")
                    return str(v)
                room_id: str = _b(row["ChatRoomName"])
                user_list: str = _b(row["UserNameList"])
                name_list: str = _b(row["DisplayNameList"])

                # 处理分隔符（实测两种都有）
                sep = self._MEMBER_SEP2 if self._MEMBER_SEP2 in user_list else self._MEMBER_SEP
                users = [u for u in user_list.replace("\x07", "^G").split(sep) if u]
                names = [n for n in name_list.replace("\x07", "^G").split(sep)]

                member_map: dict[str, str] = {}
                for idx, wxid in enumerate(users):
                    dn = names[idx] if idx < len(names) else ""
                    member_map[wxid] = dn
                cache[room_id] = member_map
        except Exception as exc:
            logger.warning("构建成员缓存失败: %s", exc)
        return cache

    def _build_contact_cache(
        self, conn: sqlite3.Connection
    ) -> dict[str, str]:
        """
        构建 {wxid: 昵称} 的联系人昵称缓存。
        用于补全 ChatRoom.DisplayNameList 为空的成员名。
        """
        cache: dict[str, str] = {}
        cur = conn.cursor()
        try:
            cur.execute("SELECT UserName, NickName, Remark FROM Contact")
            for row in cur.fetchall():
                def _b(v) -> str:
                    if v is None: return ""
                    if isinstance(v, bytes): return v.decode("utf-8", errors="ignore")
                    return str(v)
                wxid: str = _b(row["UserName"])
                name: str = _b(row["Remark"]) or _b(row["NickName"])
                if wxid:
                    cache[wxid] = name
        except Exception as exc:
            logger.warning("构建联系人缓存失败: %s", exc)
        return cache

    def _resolve_sender_name(
        self,
        sender_id: str,
        member_cache: dict[str, str],
        contact_cache: dict[str, str],
    ) -> str:
        """按优先级获取发送者显示名"""
        if sender_id == self._my_wxid:
            return contact_cache.get(sender_id, "我")
        return (
            member_cache.get(sender_id)
            or contact_cache.get(sender_id)
            or sender_id
        )

    def _row_to_message(
        self,
        row: sqlite3.Row,
        member_cache: dict[str, str],
        contact_cache: dict[str, str],
    ) -> WeChatMessage:
        """将 MSG 表一行转为 WeChatMessage 对象"""
        is_sender: bool = bool(row["IsSender"])
        # 发送者 wxid 处理
        if is_sender:
            sender_id = self._my_wxid
        else:
            sender_id = _parse_sender_from_bytes_extra(row["BytesExtra"])

        sender_name = self._resolve_sender_name(
            sender_id, member_cache, contact_cache
        )
        create_time = datetime.fromtimestamp(row["CreateTime"])

        def _s(v) -> str:
            if v is None: return ""
            if isinstance(v, bytes): return v.decode("utf-8", errors="ignore")
            return str(v)

        return WeChatMessage(
            local_id=row["localId"],
            msg_svr_id=row["MsgSvrID"],
            room_id=_s(row["StrTalker"]),
            sender_id=sender_id,
            sender_name=sender_name,
            is_sender=is_sender,
            msg_type=row["Type"],
            content=_s(row["StrContent"]),
            create_time=create_time,
            display_content=_s(row["DisplayContent"]),
        )

    # ── 公开接口 ──────────────────────────────

    def get_groups(self) -> list[WeChatGroup]:
        """
        获取所有群聊列表，按群名称排序。

        Returns:
            WeChatGroup 列表（不含非群聊联系人）。
        """
        groups: list[WeChatGroup] = []
        conn = self._connect()
        try:
            cur = conn.cursor()
            # 联结 Contact（群名/备注）与 ChatRoom（成员数）
            cur.execute("""
                SELECT
                    c.UserName       AS room_id,
                    c.NickName       AS nick_name,
                    c.Remark         AS remark,
                    cr.UserNameList  AS user_list,
                    COALESCE(cri.Announcement, '') AS announcement
                FROM Contact c
                LEFT JOIN ChatRoom cr ON c.UserName = cr.ChatRoomName
                LEFT JOIN ChatRoomInfo cri ON c.UserName = cri.ChatRoomName
                WHERE c.UserName LIKE '%@chatroom'
                ORDER BY c.Remark, c.NickName
            """)
            for row in cur.fetchall():
                def _to_str(v) -> str:
                    """将数据库字段值统一转为 str（兼容 bytes/None）"""
                    if v is None:
                        return ""
                    if isinstance(v, bytes):
                        return v.decode("utf-8", errors="ignore")
                    return str(v)

                room_id: str = _to_str(row["room_id"])
                nick_name: str = _to_str(row["nick_name"])
                remark: str = _to_str(row["remark"])
                user_list: str = _to_str(row["user_list"])
                announcement: str = _to_str(row["announcement"])

                # 计算成员数（分隔符容错）
                sep = self._MEMBER_SEP2 if self._MEMBER_SEP2 in user_list else self._MEMBER_SEP
                members = [u for u in user_list.replace("\x07", "^G").split(sep) if u]

                groups.append(WeChatGroup(
                    room_id=room_id,
                    name=nick_name,
                    remark=remark,
                    member_count=len(members),
                    announcement=announcement,
                ))

            logger.info("共获取到 %d 个群聊", len(groups))
        except Exception as exc:
            logger.error("获取群聊列表失败: %s", exc, exc_info=True)
            raise
        finally:
            conn.close()
        return groups

    def get_messages(
        self,
        room_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None,
        msg_types: Optional[set[int]] = None,
        include_system: bool = False,
    ) -> list[WeChatMessage]:
        """
        读取指定群的消息列表。

        Args:
            room_id:       群 ID（如 "12345678@chatroom"）。
            start_time:    起始时间（含），为 None 则不限。
            end_time:      结束时间（含），为 None 则不限。
            limit:         最多返回条数，为 None 则不限。
            msg_types:     消息类型白名单，None 则使用默认过滤集。
            include_system: 是否包含系统消息（Type=10000）。

        Returns:
            按时间升序排列的 WeChatMessage 列表。
        """
        conn = self._connect()
        try:
            member_cache_all = self._build_member_cache(conn)
            contact_cache = self._build_contact_cache(conn)
            member_cache = member_cache_all.get(room_id, {})

            # 构造 SQL
            conditions = ["StrTalker = ?"]
            params: list = [room_id]

            if start_time is not None:
                conditions.append("CreateTime >= ?")
                params.append(int(start_time.timestamp()))

            if end_time is not None:
                conditions.append("CreateTime <= ?")
                params.append(int(end_time.timestamp()))

            # 消息类型过滤
            allowed_types = msg_types if msg_types is not None else self._USEFUL_MSG_TYPES
            if not include_system:
                allowed_types = allowed_types - {10000}
            if allowed_types:
                placeholders = ",".join("?" * len(allowed_types))
                conditions.append(f"Type IN ({placeholders})")
                params.extend(sorted(allowed_types))

            where_clause = " AND ".join(conditions)
            order_clause = "ORDER BY CreateTime ASC"
            limit_clause = f"LIMIT {int(limit)}" if limit else ""

            sql = f"""
                SELECT localId, MsgSvrID, StrTalker, IsSender, Type,
                       StrContent, DisplayContent, CreateTime, BytesExtra
                FROM MSG
                WHERE {where_clause}
                {order_clause}
                {limit_clause}
            """
            cur = conn.cursor()
            cur.execute(sql, params)

            messages: list[WeChatMessage] = []
            for row in cur.fetchall():
                try:
                    messages.append(self._row_to_message(row, member_cache, contact_cache))
                except Exception as exc:
                    logger.debug("跳过异常消息 localId=%s: %s", row["localId"], exc)

            logger.info(
                "群 %s 获取到 %d 条消息（时间范围: %s ~ %s，limit=%s）",
                room_id, len(messages),
                start_time.strftime("%Y-%m-%d %H:%M") if start_time else "无限制",
                end_time.strftime("%Y-%m-%d %H:%M") if end_time else "无限制",
                limit,
            )
            return messages

        except Exception as exc:
            logger.error("读取群消息失败 room_id=%s: %s", room_id, exc, exc_info=True)
            raise
        finally:
            conn.close()

    def get_recent_messages(
        self,
        room_id: str,
        count: int = 200,
        include_system: bool = False,
    ) -> list[WeChatMessage]:
        """
        获取最近 N 条消息（按时间降序取，然后反转为升序返回）。

        Args:
            room_id:       群 ID。
            count:         消息条数上限，默认 200。
            include_system: 是否包含系统消息。

        Returns:
            按时间升序排列的 WeChatMessage 列表。
        """
        conn = self._connect()
        try:
            member_cache_all = self._build_member_cache(conn)
            contact_cache = self._build_contact_cache(conn)
            member_cache = member_cache_all.get(room_id, {})

            allowed_types = self._USEFUL_MSG_TYPES.copy()
            if not include_system:
                allowed_types -= {10000}

            placeholders = ",".join("?" * len(allowed_types))
            sql = f"""
                SELECT localId, MsgSvrID, StrTalker, IsSender, Type,
                       StrContent, DisplayContent, CreateTime, BytesExtra
                FROM MSG
                WHERE StrTalker = ?
                  AND Type IN ({placeholders})
                ORDER BY CreateTime DESC
                LIMIT ?
            """
            params = [room_id] + list(sorted(allowed_types)) + [int(count)]
            cur = conn.cursor()
            cur.execute(sql, params)

            messages: list[WeChatMessage] = []
            for row in cur.fetchall():
                try:
                    messages.append(self._row_to_message(row, member_cache, contact_cache))
                except Exception as exc:
                    logger.debug("跳过异常消息: %s", exc)

            # 反转恢复时间升序
            messages.reverse()
            logger.info("群 %s 最近 %d 条消息已读取", room_id, len(messages))
            return messages

        except Exception as exc:
            logger.error("读取最近消息失败 room_id=%s: %s", room_id, exc, exc_info=True)
            raise
        finally:
            conn.close()

    def search(
        self,
        keyword: str,
        room_ids: Optional[list[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[WeChatMessage]:
        """
        在群聊消息中全文搜索关键词（仅文字消息 Type=1）。

        Args:
            keyword:   搜索关键词。
            room_ids:  限定搜索的群 ID 列表，None 则搜索所有群。
            start_time: 时间范围起始（含），None 则不限。
            end_time:   时间范围结束（含），None 则不限。
            limit:     最多返回条数，默认 200。

        Returns:
            按时间升序排列的 WeChatMessage 列表。
        """
        if not keyword or not keyword.strip():
            raise ValueError("关键词不能为空")

        conn = self._connect()
        try:
            member_cache_all = self._build_member_cache(conn)
            contact_cache = self._build_contact_cache(conn)

            conditions = ["Type = 1", "StrContent LIKE ?"]
            params: list = [f"%{keyword}%"]

            if room_ids:
                placeholders = ",".join("?" * len(room_ids))
                conditions.append(f"StrTalker IN ({placeholders})")
                params.extend(room_ids)
            else:
                # 只搜索群聊，过滤掉私聊和订阅号
                conditions.append("StrTalker LIKE '%@chatroom'")

            if start_time is not None:
                conditions.append("CreateTime >= ?")
                params.append(int(start_time.timestamp()))

            if end_time is not None:
                conditions.append("CreateTime <= ?")
                params.append(int(end_time.timestamp()))

            where_clause = " AND ".join(conditions)
            sql = f"""
                SELECT localId, MsgSvrID, StrTalker, IsSender, Type,
                       StrContent, DisplayContent, CreateTime, BytesExtra
                FROM MSG
                WHERE {where_clause}
                ORDER BY CreateTime ASC
                LIMIT ?
            """
            params.append(int(limit))

            cur = conn.cursor()
            cur.execute(sql, params)

            messages: list[WeChatMessage] = []
            for row in cur.fetchall():
                try:
                    room_id = row["StrTalker"]
                    member_cache = member_cache_all.get(room_id, {})
                    messages.append(self._row_to_message(row, member_cache, contact_cache))
                except Exception as exc:
                    logger.debug("跳过异常消息: %s", exc)

            logger.info(
                "搜索关键词 '%s' 找到 %d 条消息（groups=%s）",
                keyword, len(messages),
                len(room_ids) if room_ids else "全部"
            )
            return messages

        except Exception as exc:
            logger.error("关键词搜索失败 keyword=%s: %s", keyword, exc, exc_info=True)
            raise
        finally:
            conn.close()

    def get_message_count(
        self,
        room_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        """
        统计群聊消息总数（用于前端预估）。

        Args:
            room_id:    群 ID。
            start_time: 时间范围起始。
            end_time:   时间范围结束。

        Returns:
            消息总数（int）。
        """
        conn = self._connect()
        try:
            conditions = ["StrTalker = ?", "Type IN (1,3,34,43,47,48,49)"]
            params: list = [room_id]
            if start_time:
                conditions.append("CreateTime >= ?")
                params.append(int(start_time.timestamp()))
            if end_time:
                conditions.append("CreateTime <= ?")
                params.append(int(end_time.timestamp()))
            where_clause = " AND ".join(conditions)
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM MSG WHERE {where_clause}", params)
            count = cur.fetchone()[0]
            return count
        finally:
            conn.close()


# ──────────────────────────────────────────────
# 工厂函数（供其他模块快速创建实例）
# ──────────────────────────────────────────────

def create_reader_from_config(conf_path: Optional[str] = None) -> WeChatReader:
    """
    从 PyWxDump 生成的 conf_auto.json 中读取配置，创建 WeChatReader。

    Args:
        conf_path: conf_auto.json 的路径。
                   None 则使用默认路径（项目根目录下的 wxdump_work/conf_auto.json）。

    Returns:
        WeChatReader 实例。

    Raises:
        FileNotFoundError: 配置文件或数据库文件不存在。
        KeyError: 配置文件格式不符合预期。
    """
    if conf_path is None:
        # 默认路径：exe 所在目录 / wxdump_work / conf_auto.json
        conf_path = str(_app_root() / "wxdump_work" / "conf_auto.json")

    conf_file = Path(conf_path)
    if not conf_file.exists():
        raise FileNotFoundError(f"conf_auto.json 不存在: {conf_path}")

    with open(conf_file, "r", encoding="utf-8") as f:
        conf = json.load(f)

    # 找到 last 用户的配置节
    auto_setting = conf.get("auto_setting", {})
    last_wxid: str = auto_setting.get("last", "")
    if not last_wxid:
        raise KeyError("conf_auto.json 中未找到 auto_setting.last 字段")

    user_conf = conf.get(last_wxid, {})
    merge_path: str = user_conf.get("merge_path", "")
    my_wxid: str = user_conf.get("my_wxid", last_wxid)

    if not merge_path:
        raise KeyError(f"conf_auto.json 中 {last_wxid}.merge_path 为空")

    # 相对路径 → 相对于 exe/项目根目录解析
    merge_path_obj = Path(merge_path)
    if not merge_path_obj.is_absolute():
        merge_path = str(_app_root() / merge_path_obj)

    logger.info("从配置文件加载：wxid=%s, db=%s", my_wxid, merge_path)
    return WeChatReader(db_path=merge_path, my_wxid=my_wxid)


# ── 全局单例管理器 ────────────────────────────────

_global_reader: WeChatReader | None = None
_global_reader_error: str = ""

def reset_global_reader() -> None:
    """同步完成后调用，强制下次 get_global_reader() 重新打开新的 db 文件"""
    global _global_reader, _global_reader_error
    _global_reader = None
    _global_reader_error = ""
    logger.info("WeChatReader 单例已重置，下次访问将重新加载数据库")


def _get_merge_path() -> str:
    """从 conf_auto.json 读取 merge_path（绝对路径）"""
    import json as _json, os as _os
    conf_path = str(_app_root() / "wxdump_work" / "conf_auto.json")
    with open(conf_path, encoding="utf-8") as f:
        conf = _json.load(f)
    last_wxid = conf.get("auto_setting", {}).get("last", "")
    merge_path = conf.get(last_wxid, {}).get("merge_path", "")
    if not _os.path.isabs(merge_path):
        merge_path = str(_app_root() / merge_path)
    return merge_path

def get_global_reader() -> WeChatReader:
    """懒加载 WeChatReader 全局单例；失败时抛出 RuntimeError"""
    global _global_reader, _global_reader_error
    if _global_reader is None and not _global_reader_error:
        try:
            from config import get_config
            conf_path = get_config().get_wechat_conf_path() or None
            _global_reader = create_reader_from_config(conf_path)
        except Exception as exc:
            _global_reader_error = str(exc)
            logger.error("WeChatReader 初始化失败: %s", exc)
    if _global_reader_error:
        raise RuntimeError(f"微信数据库未就绪：{_global_reader_error}")
    return _global_reader  # type: ignore

def _apply_wal_to_db(db_path: str, dst_path: str) -> None:
    """
    将 db_path 对应的 .db-wal 帧 patch 进 dst_path（db_path 的副本），
    使解密工具能读到 WAL 中尚未 checkpoint 的最新数据。

    SQLite WAL 格式：
      - 32 字节文件头
      - 每帧 = 24 字节帧头 + PAGE_SIZE 字节加密页数据
    帧头前 4 字节是 1-based 页号。

    微信加密 db 的特殊页布局：
      - db[0:16]    = 16 字节 AES 盐值（salt），PyWxDump 解密时用来派生密钥
      - db[16:4096] = 第 1 页的加密数据（4080 字节，跳过 salt）
      - db[4096:N]  = 第 2 页及后续页，每页 4096 字节，正常对齐

    WAL 帧里存的是「完整 4096 字节加密页」，其中：
      - page 1 帧的前 16 字节 = 与主库相同的 salt（由微信写入）
      - page 1 帧的 [16:4096] = 第 1 页真正的加密数据

    ★ 正确的 patch 规则：
      - page 1：seek 到 db offset 16，写 wal_page[16:4096]（4080 字节）
                → 跳过 WAL 帧中的 salt 副本，保留 dst_path 里的原始 salt
      - page N (N>=2)：seek 到 db offset (N-1)*4096，写完整 4096 字节
    """
    import os
    import struct

    PAGE_SIZE = 4096
    SALT_SIZE = 16          # 微信加密 db 第 1 页前缀：AES salt
    WAL_HEADER = 32
    FRAME_HEADER = 24

    wal_path = db_path + "-wal"
    if not os.path.exists(wal_path):
        return
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER:
        return

    frame_size = FRAME_HEADER + PAGE_SIZE
    num_frames = (wal_size - WAL_HEADER) // frame_size
    if num_frames == 0:
        return

    # 读取所有帧：只保留每页最新一帧（靠后的 frame 覆盖靠前的）
    page_frames: dict[int, bytes] = {}
    with open(wal_path, "rb") as wf:
        wf.seek(WAL_HEADER)
        for _ in range(num_frames):
            fh = wf.read(FRAME_HEADER)
            if len(fh) < FRAME_HEADER:
                break
            page_no = struct.unpack(">I", fh[:4])[0]
            page_data = wf.read(PAGE_SIZE)
            if len(page_data) < PAGE_SIZE:
                break
            if page_no >= 1:
                page_frames[page_no] = page_data

    if not page_frames:
        return

    logger.info("WAL patch: %s 包含 %d 帧，覆盖 %d 个不同页", wal_path, num_frames, len(page_frames))

    with open(dst_path, "r+b") as dbf:
        for page_no, page_data in page_frames.items():
            if page_no == 1:
                # 第 1 页特殊处理：跳过 salt，只写加密数据部分
                dbf.seek(SALT_SIZE)
                dbf.write(page_data[SALT_SIZE:])   # 写 page_data[16:4096]，4080 字节
            else:
                dbf.seek((page_no - 1) * PAGE_SIZE)
                dbf.write(page_data)


def _prepare_wx_path_with_wal(wx_path: str, tmp_dir: str) -> str:
    """
    把 wx_path 下的所有 db 文件复制到 tmp_dir（保持相对目录结构），
    并将对应的 .db-wal 帧 patch 进副本，返回 tmp_dir 内对应 wx_path 的目录。
    """
    import os
    import shutil

    wx_path_abs = os.path.abspath(wx_path)
    # tmp_dir 内创建与 wx_path 同名的子目录，保持 get_core_db 的路径解析逻辑
    wx_basename = os.path.basename(wx_path_abs)
    dst_wx = os.path.join(tmp_dir, wx_basename)

    for root, dirs, files in os.walk(wx_path_abs):
        for fname in files:
            if not fname.endswith(".db"):
                continue
            src = os.path.join(root, fname)
            rel = os.path.relpath(root, wx_path_abs)
            dst_dir = os.path.join(dst_wx, rel)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, fname)
            shutil.copy2(src, dst)
            _apply_wal_to_db(src, dst)

    return dst_wx


def _copy_wechat_snapshot(wx_path: str, tmp_dir: str) -> str:
    """
    复制微信原始数据库目录到临时目录，连同 WAL/SHM 一起形成一致快照。
    """
    import os
    import shutil
    import time

    wx_path_abs = os.path.abspath(wx_path)
    if not os.path.isdir(wx_path_abs):
        raise DatabaseSyncError(f"微信数据库目录不存在: {wx_path}")

    dst_root = os.path.join(tmp_dir, "wx_snapshot")
    copied = 0
    failed: list[str] = []
    suffixes = (".db", ".db-wal", ".db-shm")

    for root, _, files in os.walk(wx_path_abs):
        rel = os.path.relpath(root, wx_path_abs)
        dst_dir = os.path.join(dst_root, rel)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in files:
            if not fname.lower().endswith(suffixes):
                continue
            src = os.path.join(root, fname)
            dst = os.path.join(dst_dir, fname)
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                    last_exc = None
                    break
                except OSError as exc:
                    last_exc = exc
                    logger.warning(
                        "复制微信数据库快照失败，第 %d 次重试: %s -> %s: %s",
                        attempt + 1, src, dst, exc,
                    )
                    time.sleep(0.25 * (attempt + 1))
            if last_exc is not None:
                failed.append(f"{src}: {last_exc}")

    if failed:
        raise DatabaseSyncError("复制微信数据库快照失败：" + "；".join(failed[:3]))
    if copied == 0:
        raise DatabaseSyncError(f"未在微信目录中找到可同步的 SQLite 数据库: {wx_path}")

    logger.info("微信数据库快照复制完成：%s，共 %d 个文件", dst_root, copied)
    return dst_root


def _validate_merged_db(db_path: str) -> None:
    """校验合并数据库是否完整且包含必要表。"""
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            conn.execute("PRAGMA query_only = ON")
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise DatabaseSyncError(f"merge_all.db integrity_check 失败: {check[0] if check else '无结果'}")

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = {"MSG", "Contact", "ChatRoom"} - tables
            if missing:
                raise DatabaseSyncError(f"merge_all.db 缺少必要表: {', '.join(sorted(missing))}")
        finally:
            conn.close()
    except DatabaseSyncError:
        raise
    except Exception as exc:
        raise DatabaseSyncError(f"校验 merge_all.db 失败: {exc}") from exc


def _replace_database_atomically(pending_path: str, merge_path: str) -> None:
    """在全局锁内用 pending 文件替换正式数据库。"""
    import os
    import shutil

    with _db_access_lock:
        target = Path(merge_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        for suffix in ("-wal", "-shm"):
            sidecar = str(target) + suffix
            if os.path.exists(sidecar):
                try:
                    os.remove(sidecar)
                except OSError as exc:
                    raise DatabaseSyncError(f"删除旧数据库旁路文件失败: {sidecar}: {exc}") from exc

        backup = target.with_suffix(target.suffix + ".bak")
        try:
            if target.exists():
                shutil.copy2(target, backup)
            os.replace(pending_path, merge_path)
            if backup.exists():
                try:
                    backup.unlink()
                except OSError as exc:
                    logger.warning("删除数据库备份失败，可手动清理 %s: %s", backup, exc)
        except Exception as exc:
            if backup.exists():
                try:
                    shutil.copy2(backup, target)
                except OSError as restore_exc:
                    logger.error("恢复旧 merge_all.db 失败: %s", restore_exc, exc_info=True)
            raise DatabaseSyncError(f"替换 merge_all.db 失败: {exc}") from exc


def sync_database() -> tuple[bool, str]:
    """
    使用 PyWxDump 的 decrypt_merge 同步微信最新数据库到本地 merge_all.db。
    先复制微信原始库到临时目录，再输出 pending 数据库；校验通过后原子替换。
    """
    import json
    import os
    import tempfile
    try:
        from pywxdump import decrypt_merge
    except ImportError:
        logger.exception("同步失败：未安装 pywxdump")
        return False, "未安装 pywxdump"

    if not _sync_lock.acquire(blocking=False):
        msg = "数据库同步正在进行中，请稍后再试"
        logger.warning(msg)
        return False, msg

    temp_root = ""
    try:
        conf_path = str(_app_root() / "wxdump_work" / "conf_auto.json")
        if not os.path.exists(conf_path):
            raise DatabaseSyncError(f"配置文件不存在: {conf_path}")

        with open(conf_path, "r", encoding="utf-8") as f:
            conf = json.load(f)

        wxid = conf.get('auto_setting', {}).get('last', '')
        if not wxid:
            raise DatabaseSyncError("conf_auto.json 中未找到 wxid")

        user_conf = conf.get(wxid, {})
        wx_path = user_conf.get('wx_path', '')
        key = user_conf.get('key', '')
        merge_path = user_conf.get('merge_path', '')

        if not wx_path or not key or not merge_path:
            raise DatabaseSyncError("缺失 wx_path, key 或 merge_path")

        if not os.path.isabs(merge_path):
            merge_path = str(_app_root() / merge_path)

        import hashlib
        import shutil
        import struct
        import sqlite3 as _sqlite3
        from Cryptodome.Cipher import AES
        from pywxdump import decrypt, get_core_db

        os.makedirs(os.path.dirname(merge_path), exist_ok=True)
        temp_root = tempfile.mkdtemp(prefix="wechat-summary-sync-")
        snapshot_path = _copy_wechat_snapshot(wx_path, temp_root)
        pending_path = os.path.join(os.path.dirname(merge_path), "merge_all.pending.db")
        if os.path.exists(pending_path):
            os.remove(pending_path)

        # ── Step 1: decrypt_merge 获取基础数据 ──────────────────
        success, msg = decrypt_merge(
            wx_path=snapshot_path,
            key=key,
            outpath=os.path.dirname(merge_path),
            merge_save_path=pending_path,
            db_type=['MSG', 'MediaMsg', 'MicroMsg']
        )
        if not success:
            raise DatabaseSyncError(f"PyWxDump decrypt_merge 失败: {msg}")
        if not os.path.exists(pending_path):
            raise DatabaseSyncError(f"PyWxDump 未生成 pending 数据库: {pending_path}")

        # ── Step 2: WAL 补丁 —— 把加密 WAL 解密后 patch 进合并库 ──
        # decrypt_merge 只解密了 .db 文件，WAL 里的最新帧没有被纳入。
        # 对每个 MSG*.db，若存在 .db-wal 且非空，解密其帧并直接写入 pending 数据库。
        PAGE_SIZE  = 4096
        WAL_HDR    = 32
        FRAME_HDR  = 24
        password   = bytes.fromhex(key)

        code, db_infos = get_core_db(snapshot_path, ['MSG'])
        if not code:
            logger.warning("WAL patch: get_core_db 失败，跳过 WAL 补丁；基础库仍会校验并替换")
        else:
            patched_any = False
            for info in db_infos:
                db_src = info['db_path']
                wal_src = db_src + '-wal'
                if not os.path.exists(wal_src) or os.path.getsize(wal_src) <= WAL_HDR:
                    continue

                wal_size = os.path.getsize(wal_src)
                n_frames = (wal_size - WAL_HDR) // (FRAME_HDR + PAGE_SIZE)
                if n_frames == 0:
                    continue

                with open(db_src, 'rb') as f:
                    salt = f.read(16)
                aes_key = hashlib.pbkdf2_hmac('sha1', password, salt, 64000, 32)

                pages: dict[int, bytes] = {}
                with open(wal_src, 'rb') as wf:
                    wf.seek(WAL_HDR)
                    for _ in range(n_frames):
                        fh = wf.read(FRAME_HDR)
                        if len(fh) < FRAME_HDR:
                            break
                        pg_no = struct.unpack('>I', fh[:4])[0]
                        enc = wf.read(PAGE_SIZE)
                        if len(enc) < PAGE_SIZE:
                            break
                        iv = enc[-48:-32]
                        dec = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(enc[:-48])
                        pages[pg_no] = dec

                if not pages:
                    continue

                logger.info(
                    "WAL patch: %s -> %d 帧, %d 页",
                    os.path.basename(db_src), n_frames, len(pages),
                )

                wal_tmpdir = tempfile.mkdtemp(dir=temp_root)
                try:
                    tmp_db = os.path.join(wal_tmpdir, os.path.basename(db_src))
                    ok, decrypt_msg = decrypt(key, db_src, tmp_db)
                    if not ok:
                        raise DatabaseSyncError(f"WAL patch 解密 {db_src} 失败: {decrypt_msg}")

                    with open(tmp_db, 'r+b') as dbf:
                        db_size = os.path.getsize(tmp_db)
                        for pg_no, dec in pages.items():
                            if pg_no == 1:
                                continue
                            off = (pg_no - 1) * PAGE_SIZE
                            if off + PAGE_SIZE <= db_size:
                                dbf.seek(off)
                                dbf.write(dec[:4048])

                    src_conn = _sqlite3.connect(f'file:{tmp_db}?mode=ro', uri=True)
                    dst_conn = _sqlite3.connect(pending_path, timeout=30)
                    try:
                        dst_conn.execute("PRAGMA busy_timeout = 30000")
                        max_ts = dst_conn.execute(
                            "SELECT MAX(CreateTime) FROM MSG"
                        ).fetchone()[0] or 0
                        rows = src_conn.execute(
                            "SELECT * FROM MSG WHERE CreateTime > ?", (max_ts,)
                        ).fetchall()

                        if rows:
                            ncols = len(rows[0])
                            placeholders = ','.join(['?'] * ncols)
                            dst_conn.executemany(
                                f"INSERT OR IGNORE INTO MSG VALUES ({placeholders})", rows
                            )
                            dst_conn.commit()
                            logger.info(
                                "WAL patch: 从 %s 补入 %d 条新消息",
                                os.path.basename(db_src), len(rows),
                            )
                            patched_any = True
                    finally:
                        src_conn.close()
                        dst_conn.close()
                except Exception as wal_exc:
                    logger.warning("WAL patch 处理 %s 出错，跳过该库: %s", db_src, wal_exc, exc_info=True)
                finally:
                    shutil.rmtree(wal_tmpdir, ignore_errors=True)

            if patched_any:
                logger.info("WAL patch 完成，pending 数据库已更新最新消息")

        _validate_merged_db(pending_path)
        _replace_database_atomically(pending_path, merge_path)
        logger.info("数据库同步完成，已替换 merge_all.db: %s", merge_path)
        return True, merge_path

    except Exception as exc:
        logger.error("sync_database 失败: %s", exc, exc_info=True)
        return False, str(exc)
    finally:
        if temp_root:
            try:
                import shutil
                shutil.rmtree(temp_root, ignore_errors=True)
            except Exception as exc:
                logger.warning("清理同步临时目录失败 %s: %s", temp_root, exc)
        _sync_lock.release()
