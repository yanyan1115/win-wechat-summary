# Windows 安装与开发环境配置

本文面向从源码运行或参与开发的用户。普通用户优先下载 Release 中的 `WeChat-Summary.exe`。

## 环境要求

- Windows 10/11 64 位
- Python 3.10 或更高版本
- 微信 PC 版已登录
- 可访问所选 AI Provider 的网络环境，Ollama 除外

## 从源码启动

```powershell
git clone https://github.com/yanyan1115/win-wechat-summary.git
cd win-wechat-summary

python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

python app.py
```

应用会启动本地 Flask 服务，并自动打开 `http://127.0.0.1:5000`。

## 首次初始化微信数据

1. 保持微信 PC 版处于登录状态。
2. 打开网页后进入设置向导。
3. 点击“自动检测微信账号”。
4. 如果提示无法读取密钥，请使用管理员身份重新启动程序。
5. 选择账号并保存，程序会生成 `wxdump_work/conf_auto.json`。
6. 点击“同步”，生成 `wxdump_work/<wxid>/merge_all.db`。

`wxdump_work/` 包含微信数据库密钥和本地消息数据，已被 `.gitignore` 忽略，严禁提交。

## 配置 AI Provider

在“系统设置”中选择 Provider，并填写 API Key 和模型名。

常用配置：

- Claude: `claude-3-5-sonnet-20241022`
- ChatGPT: `gpt-4o`
- DeepSeek: `deepseek-chat`
- 通义千问: `qwen-plus`
- Ollama: 本地模型名，例如 `qwen2.5`

API Key 会存放在 `~/.wechat-summary/config.json`，并通过 Windows DPAPI 加密。

## 打包 EXE

```powershell
.\.venv\Scripts\activate
pip install pyinstaller
pyinstaller build.spec
```

生成文件位于 `dist/WeChat-Summary.exe`。打包时需要包含 Flask 模板、静态资源和 PyWxDump 偏移量数据。

## 开发检查

```powershell
python -m py_compile app.py web\routes.py core\wechat.py core\jobs.py ai\base.py
python -m pytest test_ai_base.py -q
```
