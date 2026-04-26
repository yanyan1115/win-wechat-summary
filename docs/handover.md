# Windows 微信群聊 AI 总结工具 — 交接文档

## 1. 项目现状 (Current Status)
目前已经基本完成了 **Phase 1**（核心功能与本地 Web 界面）以及 **Phase 2**（定时任务、数据库加密与历史记录同步）。
项目已具备“自动定期同步微信数据并利用大模型生成详细总结”的闭环能力。

### 模块概览
- **入口与路由**
  - `app.py`: Flask 后端的主入口。负责启动 Web 服务器以及初始化后台任务调度器 (APScheduler)。
  - `web/routes.py`: Flask API 的所有路由实现（包括群列表获取、总结请求、历史记录管理、定时任务配置更新等）。
- **前端界面**
  - `web/templates/index.html`: 采用单文件构建的极简无构建化前端。依赖 Tailwind CSS CDN + Alpine.js。实现了所有配置页面的动态绑定、动画折叠、以及 Toast 提示反馈。
- **核心逻辑**
  - `core/wechat.py`: **WeChatReader 单例实现**，集成底层微信 SQLite 数据源，封装了获取群组、读取最新消息的方法，并提供了 `sync_database()` 用以对接 PyWxDump 手动/自动合并实时数据库。
  - `core/scheduler.py`: 后台定时任务的引擎。利用 `APScheduler` (基于本地时区) 每天自动唤醒，先同步解密合并最新数据库，再遍历设定好的群聊依次调用 AI 进行总结，并静默写入历史库。
  - `core/history.py`: 基于 `sqlite3` 实现的轻量级历史记录管理器。负责持久化存储每一次的总结结果。
- **配置与安全**
  - `config.py`: 全局配置管理。**已经支持 Windows DPAPI 底层加密 (`CryptProtectData`)**。API Key 落盘时处于系统级强加密状态，页面返回时自动脱敏，防止被误覆盖。
- **AI 适配器架构**
  - `ai/base.py`: AI 提供商基类以及各种 Prompt 模板（如“技术交流群”与“通用群”模板，支持按話題、工具資源等详细拆分）。
  - `ai/factory.py`: 模型适配工厂。
  - `ai/openai_provider.py`: 基于 `openai` SDK，兼容 ChatGPT、DeepSeek 和通义千问等模型。
  - `ai/claude_provider.py` & `ai/ollama_provider.py`: 负责对接对应的模型生态。

---

## 2. 已知 Bug (Known Bug)

### 问题描述
点击“同步”按钮（或定时任务触发同步）后，消息列表和新总结**只能读取到 4/25 11:40 之前的消息，无法读到此时间点之后的真·最新消息**。

### 原因排查与当前实现方式
- **当前同步机制：** 目前在 `core/wechat.py` 中，`sync_database()` 通过调用 `pywxdump.decrypt_merge` 函数来实现数据库的解密合并。
  - *读取配置*：它会从 `wxdump_work/conf_auto.json` 中读取 `wx_path`、`key` 和 `merge_path`。
  - *调用 API*：然后执行 `decrypt_merge(wx_path=wx_path, key=key, outpath=..., merge_save_path=merge_path, db_type=['MSG', 'MediaMsg', 'MicroMsg'])`。
- **产生问题的原因猜测：**
  - `decrypt_merge` 是静态解密方法。当微信 (WeChat.exe) 在后台运行时，最新的消息会先进入 SQLite 的 Write-Ahead Log (`.db-wal` 缓存文件) 中，或者缓存在内存里，并未立即落盘到原生的 `MSG0.db` 文件。
  - 如果 `decrypt_merge` 只是机械地复制并解密 `MSG0.db`，就会漏掉还卡在 WAL 中的数据。
  - 另一种可能是，PyWxDump 需要通过针对内存操作的 `realTime.exe` 注入或抓取（即其自带的 `all_merge_real_time_db` 方法）才能抓到最新消息，但由于本地环境限制，我们在使用该方法时进程被卡死挂起。

---

## 3. 未完成的工作 (TODO / Next Steps)

- **修复同步延迟 Bug**：
  - [ ] **方案 A**: 研究并修复 `pywxdump.all_merge_real_time_db` 卡死的问题（可能涉及到 UAC 提权或安全软件拦截拦截）。
  - [ ] **方案 B**: 检查 `wx_path` 目录下的 `.db-wal` 文件，在执行 `decrypt_merge` 时是否需要先触发 Checkpoint 或合并工具将微信 WAL 刷入主库中。
- **Phase 3 打包发布准备**：
  - [ ] 配置 `build.spec`，使用 PyInstaller 开启 `--onefile --windowed` 模式进行打包。
  - [ ] 确保 `flask` 启动必须的 `web/templates` 和 `web/static` 被正确写进打包资产（`datas` 参数）。
  - [ ] 解决前端 CDN 的依赖（若需要纯离线运行，需要将 Tailwind 和 Alpine.js 及其插件文件下载到 `static` 并在 HTML 中替换本地路径）。

---

## 4. 开发环境信息 (Environment Info)

- **开发操作系统**: Windows (需支持 64 位及 DPAPI 系统调用)
- **Python 版本**: `Python 3.11`
- **项目绝对路径**: `d:\APP\win-wechat-summary`
- **虚拟环境**: `d:\APP\win-wechat-summary\.venv`
- **启动方式**:
  ```powershell
  # 运行 Flask 主服务及后台调度器
  d:\APP\win-wechat-summary\.venv\Scripts\python.exe app.py
  ```
- **核心依赖 (requirements.txt 节选)**:
  - `flask >= 3.0`
  - `pywxdump` (后来由于 .venv 中缺失，使用 `pip install pywxdump` 在本地安装了 `3.1.46`)
  - `apscheduler >= 3.10`
  - `openai >= 1.30`
  - `anthropic >= 0.30`

---

## 5. 关键技术决策记录 (Architecture & Technical Decisions)

1. **为什么用 Alpine.js + Tailwind CSS CDN？**
   * *决策理由*：这是一个典型的“小而美”的工具。如果引入 Node.js、Vite 或 React，编译链条太重且与 PyInstaller 结合复杂。Alpine.js 在 HTML 文件里直接完成极简的数据双向绑定和组件状态控制（例如手风琴动画、Toast 通知），配合 Tailwind 提供现代、美观且极具响应感的 UI。
2. **多模型适配 (OpenAI 兼容层的魅力)**
   * *决策理由*：直接复用了 `openai` Python SDK 来支持 **DeepSeek** 和 **通义千问**。在 `ai/openai_provider.py` 内部，只需要根据前端选定的模型名，动态切换 `base_url`（如 `https://api.deepseek.com/v1`）。这使得扩展其他国产大模型变得轻而易举，无需下载各种臃肿的厂商专属 SDK。
3. **API Key 系统级加密 (DPAPI)**
   * *坑与解法*：前端将配置持久化时，为防中间人偷窥和录屏泄露，做了一个 `sk-****` 的脱敏遮罩。但这导致了重新保存配置时将 `sk-****` 覆写回后端的问题。
   * *最终方案*：后端写了防覆写过滤逻辑，同时使用 `ctypes` 调用了 Windows 原生的 `CryptProtectData`（DPAPI），加密后的密钥只能由当前登录系统的用户解密，完美解决了本地明文存储的隐私风险。
4. **多线程数据冲突 (WeChatReader 单例)**
   * *坑与解法*：后台 APScheduler 在独立线程中触发总结时，与 Flask 路由的请求极有可能同时初始化 WeChatReader 抢占 `merge_all.db` 的读写锁，导致 `OperationalError: database is locked`。
   * *最终方案*：重构并去除了各自路由里的实例化操作，改为通过全局惰性加载单例 `get_global_reader()` 进行访问。
