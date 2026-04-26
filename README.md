# 微信群聊 AI 总结 / WeChat Group AI Summarizer

> 🔒 **零封号风险** — 只读本地数据库，绝不与微信服务器通信  
> 🤖 **多模型支持** — Claude / ChatGPT / DeepSeek / 通义千问 / Ollama  
> 🖥️ **本地运行** — 所有数据留在你的电脑上，API Key 加密存储  

---

## 简介 / Introduction

**中文**：一个 Windows 桌面工具，通过 [PyWxDump](https://github.com/xaoyaoo/PyWxDump) 读取本地微信数据库，调用大语言模型对群聊消息进行智能总结。适合每天群消息太多、没时间全部看完的朋友。

**English**: A Windows desktop tool that reads your local WeChat database via [PyWxDump](https://github.com/xaoyaoo/PyWxDump) and uses large language models to generate smart summaries of group chat messages. Perfect for staying on top of busy group chats without reading every message.

---

## 功能特色 / Features

| 功能 | 说明 |
|------|------|
| 📋 **群聊总结** | 按时间段或最近 N 条消息生成 AI 总结 |
| 🔖 **阅读书签** | 自动记录上次总结位置，下次一键续读 |
| 🔍 **关键词搜索** | 跨群全文搜索 + AI 归纳搜索结果 |
| 📜 **历史记录** | 所有总结自动存档，支持导出为 Markdown |
| ⏰ **定时任务** | 每天自动总结指定群聊，静默写入历史 |
| 🤖 **多模型支持** | 自由切换 Claude / ChatGPT / DeepSeek / 通义千问 / Ollama |
| 🔐 **安全存储** | API Key 使用 Windows DPAPI 加密，只有当前用户可解密 |
| 🗂️ **系统托盘** | 关闭浏览器后程序继续在托盘运行 |

---

## 截图 / Screenshots

> *(截图待补充 / Screenshots coming soon)*

<!-- 
  建议截图：
  1. 主界面 — 群聊总结结果
  2. AI 设置页面
  3. 历史记录页面
  4. 系统托盘菜单
-->

---

## 支持的 AI 模型 / Supported AI Models

| 提供商 | 模型示例 | 说明 |
|--------|----------|------|
| **Anthropic Claude** | claude-3-5-sonnet-20241022 | 需要 Claude API Key |
| **OpenAI ChatGPT** | gpt-4o, gpt-4o-mini | 需要 OpenAI API Key |
| **DeepSeek** | deepseek-chat | 需要 DeepSeek API Key，性价比高 |
| **通义千问** | qwen-plus, qwen-turbo | 需要阿里云 API Key |
| **Ollama** | llama3, qwen2.5 等 | 本地运行，无需 API Key |

---

## 快速开始 / Quick Start

### 前提条件 / Prerequisites

1. **Windows 10/11 64位**
2. **微信 PC 版**（需曾在此电脑登录过，本地有数据库文件）
3. **PyWxDump**（用于获取微信数据库密钥）

### 方法一：直接运行 exe（推荐）/ Method 1: Run the exe (Recommended)

1. 前往 [Releases](https://github.com/yanyan1115/win-wechat-summary/releases) 下载最新的 `WeChat-Summary.exe`
2. 保持微信 PC 版处于**登录状态**
3. 双击 `WeChat-Summary.exe` 启动，浏览器会自动打开 `http://127.0.0.1:5000`
4. **首次启动**会弹出设置向导，点击「自动检测微信账号」，选择账号后确认即可完成初始化（无需任何命令行操作）
5. 在「AI 设置」中配置 API Key，点击「同步」，即可开始使用

### 方法二：从源码运行 / Method 2: Run from Source

```powershell
# 1. 克隆仓库
git clone https://github.com/yanyan1115/win-wechat-summary.git
cd win-wechat-summary

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# 3. 启动应用（首次启动时浏览器会弹出设置向导）
python app.py
```

### 方法三：自行打包 / Method 3: Build from Source

```powershell
pip install pyinstaller
pyinstaller build.spec
# 生成文件在 dist/WeChat-Summary.exe
```

---

## wxdump_work 目录结构 / wxdump_work Directory

**exe 版本**：首次启动时设置向导会自动完成初始化，无需手动操作。

**源码运行**：需在项目根目录创建 `wxdump_work/conf_auto.json`（可由 `pywxdump showkey` 生成，或直接启动 app.py 后通过设置向导自动生成）：

```
wxdump_work/
└── conf_auto.json          # 微信账号配置（含数据库密钥，自动生成）
└── wxid_xxxxxxxx/
    └── merge_all.db        # 同步后生成的合并数据库
```

---

## 配置说明 / Configuration

应用配置保存在 `~/.wechat-summary/config.json`（自动创建），其中 API Key 使用 Windows DPAPI 加密，**只有当前 Windows 登录用户才能读取**。

---

## 常见问题 / FAQ

**Q: 会不会导致微信封号？**  
A: 不会。本工具只读取本地 SQLite 数据库文件，不与微信服务器产生任何通信。

**Q: 找不到群聊 / 群列表为空？**  
A: 确认 `wxdump_work/conf_auto.json` 存在且路径正确。点击界面左上角「同步」按钮触发数据库解密合并。

**Q: 同步后消息不是最新的？**  
A: 微信在运行时，最新消息会先缓存在 WAL（预写日志）文件中，每隔几分钟才自动刷入数据库。同步成功后会显示「数据截至 HH:MM」，稍等几分钟再同步通常即可更新。  
如果长时间不更新（比如某个 MSG 数据库文件刚写满 120MB），重启一次微信 PC 版可强制刷新，之后再同步即可恢复正常。

**Q: 总结质量不好？**  
A: 可以在「AI 设置」中切换 Prompt 模板（技术交流群 / 通用群），或选择更强的模型（如 Claude Sonnet / GPT-4o）。

**Q: 如何在没有网络的环境下使用？**  
A: 前端已将 Tailwind / Alpine.js / Marked.js 打包为本地文件，无需网络。但 AI API 调用仍需联网（Ollama 除外）。

**Q: 支持 Mac / Linux 吗？**  
A: 暂不支持。微信 PC 版仅有 Windows 版本，且数据库解密依赖 Windows DPAPI。

---

## 技术栈 / Tech Stack

- **后端**: Python 3.11 + Flask
- **前端**: 单文件 HTML SPA + Tailwind CSS + Alpine.js（无 Node.js 构建）
- **数据库**: SQLite（通过 PyWxDump 解密读取）
- **AI SDK**: anthropic / openai / requests（Ollama）
- **定时任务**: APScheduler
- **打包**: PyInstaller（单文件 exe，约 37MB）

---

## 贡献 / Contributing

欢迎提 Issue 和 PR！特别欢迎：
- 新 AI Provider 适配
- Prompt 模板优化
- Bug 修复

---

## 免责声明 / Disclaimer

本工具仅供个人学习和使用，请遵守微信用户协议。作者不对任何滥用行为承担责任。

This tool is for personal use only. Please comply with WeChat's Terms of Service. The author is not responsible for any misuse.

---

## License

[MIT](LICENSE) © 2025 yanyan1115
