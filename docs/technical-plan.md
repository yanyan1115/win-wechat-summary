# Windows 微信群聊 AI 总结 — 技术方案

> 基于 PyWxDump + 多模型 AI 的本地安全方案
> 目标：打包为 exe，开箱即用，零封号风险

---

## 一、功能范围

### ✅ 做什么

| 功能 | 说明 |
|------|------|
| **群聊 AI 总结** | 选择群聊 → 按时间段/条数 → AI 生成结构化摘要 |
| **定时自动总结** | 可设置定时任务（如每天 18:00 自动总结当天消息） |
| **关键词查询** | 输入关键词 + 时间范围 → 搜索聊天记录，可选 AI 归纳 |
| **总结历史** | 保存每次总结结果，支持回看和导出 |
| **多 AI 支持** | Claude / DeepSeek / 通义千问 / ChatGPT / Ollama |
| **本地 Web 界面** | 浏览器访问 `localhost`，操作简单直观 |
| **打包分发** | 打包为单个 exe，朋友下载即用 |

### ❌ 不做什么

| 功能 | 原因 |
|------|------|
| 自动回复/发消息 | 需要操作微信界面，有封号风险 |
| 实时消息推送 | 需持续读取数据库，资源占用大，MVP 不做 |
| 图片/视频解析 | 微信 2026.3 后图片加密方式更新，暂不处理 |

---

## 二、技术架构

```
┌──────────────────────────────────────────────────┐
│                  浏览器 Web 界面                    │
│         (localhost:5000 · 选群/总结/查询/历史)       │
└───────────────────────┬──────────────────────────┘
                        │ HTTP
┌───────────────────────▼──────────────────────────┐
│              Python 后端 (Flask)                   │
│                                                    │
│  ┌────────────┐  ┌────────────┐  ┌─────────────┐ │
│  │  消息读取   │  │  AI 调用    │  │  定时任务    │ │
│  │  模块      │  │  模块       │  │  模块       │ │
│  └─────┬──────┘  └─────┬──────┘  └──────┬──────┘ │
│        │               │                │         │
│  ┌─────▼──────┐  ┌─────▼──────┐  ┌──────▼─────┐ │
│  │ PyWxDump   │  │ Claude     │  │ APScheduler│ │
│  │ 解密+读取   │  │ DeepSeek   │  │ 定时触发   │ │
│  │            │  │ 通义千问    │  │            │ │
│  │            │  │ ChatGPT    │  │            │ │
│  │            │  │ Ollama     │  │            │ │
│  └─────┬──────┘  └────────────┘  └────────────┘ │
│        │                                          │
│  ┌─────▼──────────────────────────────────────┐  │
│  │         微信本地 SQLite 数据库（只读）         │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

**安全原理**：全程只读本地数据库文件，不与微信服务器通信，微信完全感知不到。

---

## 三、技术选型

| 层面 | 选型 | 理由 |
|------|------|------|
| **微信数据解密** | PyWxDump | Windows 微信密钥提取最成熟的方案，社区活跃 |
| **后端框架** | Flask | 轻量，打包体积小，够用 |
| **前端界面** | 内嵌 HTML + Tailwind CSS | 单文件 SPA，不需要 Node.js 构建 |
| **AI 调用** | 各家官方 SDK / HTTP API | anthropic、openai、dashscope 等 |
| **定时任务** | APScheduler | Python 原生，嵌入 Flask 方便 |
| **数据存储** | SQLite（总结历史等） | 轻量，不需要额外装数据库 |
| **打包工具** | PyInstaller | 打包为单 exe，用户无需装 Python |

---

## 四、项目结构

```
win-wechat-summary/
├── app.py                    # 主入口：启动 Flask + 打开浏览器
├── config.py                 # 配置管理（API Key、默认模型等）
│
├── core/
│   ├── wechat.py             # 微信数据读取（基于 PyWxDump）
│   │                         #   - 获取群聊列表
│   │                         #   - 按时间/条数读取消息
│   │                         #   - 关键词搜索
│   ├── scheduler.py          # 定时任务管理
│   └── history.py            # 总结历史存储与查询
│
├── ai/
│   ├── base.py               # AI Provider 基类 + Prompt 模板
│   ├── factory.py            # 工厂函数：根据配置创建 provider
│   ├── claude_provider.py    # Claude (Anthropic API)
│   ├── openai_provider.py    # ChatGPT / DeepSeek / 通义（OpenAI 兼容）
│   └── ollama_provider.py    # Ollama 本地模型
│
├── web/
│   ├── routes.py             # Flask 路由（API 接口）
│   ├── templates/
│   │   └── index.html        # 单页面应用（SPA）
│   └── static/               # 静态资源
│
├── resources/
│   └── icon.ico              # 应用图标
│
├── build.spec                # PyInstaller 打包配置
├── requirements.txt          # Python 依赖
├── 使用说明.md                # 用户文档
└── README.md
```

---

## 五、核心模块设计

### 5.1 微信数据读取 (`core/wechat.py`)

```python
# 核心接口设计（伪代码）

class WeChatReader:
    def __init__(self):
        """初始化：调用 PyWxDump 解密数据库"""
        # 1. 获取微信进程 PID
        # 2. 从内存提取密钥
        # 3. 解密数据库到临时目录
        # 4. 建立只读连接

    def get_groups(self) -> list[Group]:
        """获取所有群聊列表"""

    def get_messages(
        self,
        group_id: str,
        start_time: datetime = None,
        end_time: datetime = None,
        limit: int = None,
        keyword: str = None
    ) -> list[Message]:
        """读取群聊消息，支持时间范围/条数/关键词筛选"""

    def search(
        self,
        keyword: str,
        group_ids: list[str] = None,
        start_time: datetime = None,
        end_time: datetime = None
    ) -> list[Message]:
        """跨群搜索消息"""
```

**注意事项**：
- 必须以只读模式打开数据库（`?mode=ro`），避免任何写入
- 微信运行时会锁定数据库，需要用 WAL 模式或拷贝后读取
- PyWxDump 已封装好大部分逻辑，尽量复用其 API

### 5.2 AI 调用 (`ai/`)

```python
# 基类设计
class AIProvider(ABC):
    @abstractmethod
    async def summarize(self, messages: list[Message], prompt: str) -> str:
        """生成消息总结"""

    @abstractmethod
    async def query(self, messages: list[Message], question: str) -> str:
        """基于消息回答问题"""

# Prompt 模板（参考原 Mac 项目）
SUMMARY_PROMPT = """
你是一个群聊消息总结助手。请对以下群聊消息进行总结。

要求：
1. 按话题分类整理
2. 提取关键信息和结论
3. 标注重要的 @提及 和待办事项
4. 忽略无意义的闲聊和表情

消息记录：
{messages}
"""
```

**多模型支持策略**：
- Claude → 用 `anthropic` SDK
- DeepSeek / 通义千问 → 它们都兼容 OpenAI API 格式，用 `openai` SDK 改 base_url
- ChatGPT → 用 `openai` SDK
- Ollama → HTTP 调用 `localhost:11434`

这样实际只需要写 3 个 Provider（Claude、OpenAI 兼容、Ollama），就能覆盖 5+ 个模型。

### 5.3 Web 界面 (`web/`)

单页面应用，主要页面：

| 页面/视图 | 功能 |
|-----------|------|
| **设置页** | 配置 API Key、选择默认模型、设置定时任务 |
| **群聊列表** | 显示所有群，支持搜索、分组 |
| **总结页** | 选群 → 选时间范围 → 点击总结 → 显示结果 |
| **搜索页** | 关键词 + 时间范围 → 搜索结果 → 可选 AI 归纳 |
| **历史页** | 查看过去的总结记录，支持导出 |

### 5.4 打包方案

```
PyInstaller 打包策略：
├── 将 Python 运行时 + 所有依赖打包进 exe
├── 内嵌 Flask 模板和静态文件
├── 首次运行自动解密数据库
├── 配置文件保存在用户目录 (~/.wechat-summary/)
└── 目标体积：< 50MB
```

**用户使用流程**：
1. 双击 `微信群聊总结.exe`
2. 自动打开浏览器 `http://localhost:5000`
3. 首次使用：输入 API Key → 检测微信 → 自动解密
4. 选群 → 总结 → 查看结果

---

## 六、开发路线图

### Phase 1 — 核心可用（约 1~2 周）

> 目标：能跑起来，能总结

- [ ] 集成 PyWxDump，实现数据库解密和消息读取
- [ ] 实现 AI Provider（先做 Claude + OpenAI 兼容）
- [ ] Flask 后端 API（群列表、消息读取、AI 总结）
- [ ] 最简 Web 界面（能选群、能总结、能看结果）

**里程碑**：选一个群 → 点"总结" → 看到 AI 生成的摘要

### Phase 2 — 功能完善（约 1 周）

> 目标：好用、稳定

- [ ] 关键词搜索 + AI 归纳
- [ ] 总结历史保存与查看
- [ ] 定时自动总结
- [ ] 多模型切换（加上 Ollama 支持）
- [ ] 错误处理和友好提示

**里程碑**：日常可用，设好定时任务每天自动总结

### Phase 3 — 打包分发（约 3~5 天）

> 目标：朋友能用

- [ ] PyInstaller 打包为 exe
- [ ] 首次运行引导流程（检测微信、输入 Key）
- [ ] 编写使用说明文档
- [ ] 测试不同 Windows 版本兼容性（Win10/11）
- [ ] 测试不同微信版本兼容性

**里程碑**：发给朋友，双击就能用

### Phase 4 — 锦上添花（可选）

- [ ] 群聊分组管理
- [ ] 总结结果导出为 Markdown / PDF
- [ ] 阅读书签（记住上次读到哪）
- [ ] 界面美化
- [ ] 托盘图标（最小化到系统托盘）

---

## 七、关键风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 微信更新导致 PyWxDump 失效 | 🔴 致命 | 关注 PyWxDump 社区更新；密钥提取模块做好抽象，方便替换 |
| 微信运行时数据库锁定 | 🟡 功能受限 | 使用 WAL 模式读取，或拷贝数据库文件后再读 |
| PyInstaller 打包体积过大 | 🟡 体验 | 用 `--exclude-module` 排除不需要的库；考虑用 Nuitka 替代 |
| 用户 API Key 安全 | 🟡 安全 | 本地加密存储（Windows DPAPI）；不上传、不同步 |
| 不同微信版本数据库结构差异 | 🟡 兼容性 | 测试主流版本；做好版本检测和提示 |

---

## 八、依赖清单

```
# requirements.txt

# 核心
pywxdump>=3.0          # 微信数据库解密
flask>=3.0             # Web 后端

# AI 服务
anthropic>=0.30        # Claude
openai>=1.30           # ChatGPT / DeepSeek / 通义千问
requests>=2.31         # Ollama HTTP 调用

# 工具
apscheduler>=3.10      # 定时任务
cryptography>=41.0     # 密钥安全存储

# 打包
pyinstaller>=6.0       # 打包为 exe（仅开发时需要）
```

---

## 九、与原 Mac 项目的关系

| 模块 | 复用方式 |
|------|----------|
| `ai/base.py` Prompt 模板 | ✅ 直接参考，Prompt 设计思路通用 |
| `ai/` Provider 结构 | ✅ 架构参考，代码重写 |
| `core/wechat_db.py` 消息解析 | ⚠️ 参考消息类型处理逻辑，底层换成 PyWxDump |
| `core/bookmark.py` 阅读书签 | ✅ 逻辑通用，可直接移植 |
| `core/config.py` 配置管理 | ⚠️ 参考结构，存储方式改为 Windows 风格 |
| `app.py` macOS 菜单栏 | ❌ 完全不用，换成 Web 界面 |
| `c_src/` 密钥提取 | ❌ 完全不用，PyWxDump 已解决 |
