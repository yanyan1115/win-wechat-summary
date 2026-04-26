# AGENTS.md — Windows 微信群聊 AI 总结

## 项目概述

这是一个 Windows 本地应用，通过 PyWxDump 解密并只读微信本地 SQLite 数据库，调用 AI 生成群聊消息总结。最终打包为 exe 分发。

**核心原则：绝不与微信服务器通信，只读本地数据库，零封号风险。**

## 功能范围

做：
- 群聊 AI 总结（按时间段/条数）
- 定时自动总结（APScheduler）
- 关键词搜索 + AI 归纳
- 总结历史保存与回看
- 多 AI 模型支持（Claude / DeepSeek / 通义千问 / ChatGPT / Ollama）
- 本地 Web 界面（Flask + 浏览器）
- PyInstaller 打包为 exe

不做：
- 自动回复/发消息（有封号风险）
- 实时消息推送
- 图片/视频解析

## 技术栈

- Python 3.10+
- PyWxDump：微信数据库解密与消息读取
- Flask：Web 后端
- 前端：单文件 HTML SPA + Tailwind CSS CDN（不用 Node.js）
- AI SDK：anthropic（Claude）、openai（ChatGPT/DeepSeek/通义千问）、requests（Ollama）
- APScheduler：定时任务
- SQLite：总结历史存储
- PyInstaller：打包

## 项目结构

```
win-wechat-summary/
├── app.py                    # 主入口：启动 Flask + 自动打开浏览器
├── config.py                 # 配置管理（API Key、模型选择等）
├── core/
│   ├── wechat.py             # 微信数据读取（基于 PyWxDump）
│   ├── scheduler.py          # 定时任务管理
│   └── history.py            # 总结历史存储与查询
├── ai/
│   ├── base.py               # AI Provider 抽象基类 + Prompt 模板
│   ├── factory.py            # 工厂函数：根据配置创建 provider
│   ├── claude_provider.py    # Claude（anthropic SDK）
│   ├── openai_provider.py    # ChatGPT / DeepSeek / 通义千问（openai SDK，改 base_url）
│   └── ollama_provider.py    # Ollama（HTTP localhost:11434）
├── web/
│   ├── routes.py             # Flask 路由
│   ├── templates/
│   │   └── index.html        # 单页面应用
│   └── static/
├── resources/
│   └── icon.ico
├── build.spec                # PyInstaller 配置
├── requirements.txt
└── AGENTS.md
```

严格遵守此结构，新增文件前先确认放在哪个目录。

## 编码规范

### 通用
- 使用中文注释和文档字符串
- 类型注解：所有函数参数和返回值都要加 type hints
- 错误处理：不要吞掉异常，用 logging 记录，给用户友好提示
- 数据库只读：打开微信数据库时必须加 `?mode=ro`，禁止任何写入操作

### AI Provider 规范
- 所有 Provider 继承 `ai/base.py` 的 `AIProvider` 基类
- DeepSeek 和通义千问使用 openai SDK 兼容模式（只改 base_url 和 model 名）
- Prompt 模板统一放在 `ai/base.py`，不要散落在各个 Provider 里
- API Key 从 config.py 读取，不要硬编码

### Web 规范
- 前端是单个 index.html，内嵌 JS 和 CSS
- 用 Tailwind CSS CDN，不要引入 npm
- 通过 fetch 调用 Flask API，返回 JSON
- API 路由统一前缀 `/api/`

### 配置管理
- 配置文件保存在 `~/.wechat-summary/config.json`
- API Key 加密存储（Windows DPAPI）
- 提供默认值，首次运行无需配置也能进入界面

## AI 多模型实现要点

```python
# base.py 中的基类
class AIProvider(ABC):
    @abstractmethod
    async def summarize(self, messages: list, prompt: str) -> str: ...

    @abstractmethod
    async def query(self, messages: list, question: str) -> str: ...

# openai_provider.py 同时服务三个模型：
# - ChatGPT:  base_url=默认,       model="gpt-4o"
# - DeepSeek: base_url="https://api.deepseek.com/v1", model="deepseek-chat"
# - 通义千问:  base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen-plus"
```

## 打包注意事项

- 使用 PyInstaller `--onefile` 模式
- Flask 模板和静态文件要加入 `datas` 配置
- 目标体积 < 50MB
- 入口脚本 app.py 需判断是否被打包运行（`getattr(sys, 'frozen', False)`）

## 当前开发阶段：Phase 1

Phase 1 目标：核心可用——能选群、能总结、能看结果。

任务清单：
1. core/wechat.py — 集成 PyWxDump，实现获取群列表、按时间/条数读取消息
2. ai/base.py + ai/factory.py — AI 基类和工厂
3. ai/claude_provider.py + ai/openai_provider.py — 先做这两个
4. web/routes.py — Flask API（群列表、读消息、AI 总结）
5. web/templates/index.html — 最简界面（选群 → 总结 → 看结果）
6. app.py — 启动入口
