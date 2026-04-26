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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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
        uri = f"file:{self._db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=20,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 启用 WAL 读取（即使主进程锁定也能读到最新快照）
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass  # 只读模式下可能不支持，忽略
        return conn

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
                room_id: str = row["ChatRoomName"] or ""
                user_list: str = row["UserNameList"] or ""
                name_list: str = row["DisplayNameList"] or ""

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
                wxid: str = row["UserName"] or ""
                name: str = row["Remark"] or row["NickName"] or ""
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

        return WeChatMessage(
            local_id=row["localId"],
            msg_svr_id=row["MsgSvrID"],
            room_id=row["StrTalker"],
            sender_id=sender_id,
            sender_name=sender_name,
            is_sender=is_sender,
            msg_type=row["Type"],
            content=row["StrContent"] or "",
            create_time=create_time,
            display_content=row["DisplayContent"] or "",
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
                room_id: str = row["room_id"]
                nick_name: str = row["nick_name"] or ""
                remark: str = row["remark"] or ""
                user_list: str = row["user_list"] or ""
                announcement: str = row["announcement"] or ""

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
        # 默认路径：相对于本文件的上级目录
        conf_path = str(
            Path(__file__).parent.parent / "wxdump_work" / "conf_auto.json"
        )

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

    logger.info("从配置文件加载：wxid=%s, db=%s", my_wxid, merge_path)
    return WeChatReader(db_path=merge_path, my_wxid=my_wxid)


# ── 全局单例管理器 ────────────────────────────────

_global_reader: WeChatReader | None = None
_global_reader_error: str = ""

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


def sync_database() -> tuple[bool, str]:
    """
    使用 PyWxDump 的 decrypt_merge 同步微信最新数据库到本地 merge_all.db。
    在解密前先将各 .db-wal 中的帧 patch 进主库副本，确保最新消息不丢失。
    """
    import json
    import os
    import shutil
    import tempfile
    try:
        from pywxdump import decrypt_merge
    except ImportError:
        return False, "未安装 pywxdump"

    conf_path = str(Path(__file__).parent.parent / "wxdump_work" / "conf_auto.json")
    if not os.path.exists(conf_path):
        return False, f"配置文件不存在: {conf_path}"

    with open(conf_path, "r", encoding="utf-8") as f:
        conf = json.load(f)

    wxid = conf.get('auto_setting', {}).get('last', '')
    if not wxid:
        return False, "conf_auto.json 中未找到 wxid"

    user_conf = conf.get(wxid, {})
    wx_path = user_conf.get('wx_path', '')
    key = user_conf.get('key', '')
    merge_path = user_conf.get('merge_path', '')

    if not wx_path or not key or not merge_path:
        return False, "缺失 wx_path, key 或 merge_path"

    # 创建临时目录，将 db 文件连同 WAL patch 后的副本放入，再交给 decrypt_merge
    tmp_dir = tempfile.mkdtemp(prefix="wxsync_wal_")
    try:
        patched_wx_path = _prepare_wx_path_with_wal(wx_path, tmp_dir)
        logger.info("WAL patch 完成，临时目录: %s", patched_wx_path)

        success, msg = decrypt_merge(
            wx_path=patched_wx_path,
            key=key,
            outpath=os.path.dirname(merge_path),
            merge_save_path=merge_path,
            db_type=['MSG', 'MediaMsg', 'MicroMsg']
        )
        return success, msg
    except Exception as exc:
        logger.error("sync_database 失败: %s", exc, exc_info=True)
        return False, str(exc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
