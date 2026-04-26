"""
app.py — 应用主入口

启动 Flask + 自动打开浏览器。
支持 PyInstaller 打包后运行（frozen 模式）。
"""

import logging
import sys
import threading
import webbrowser
from pathlib import Path

# ── 日志配置 ─────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── 兼容 PyInstaller 打包 ────────────────────────
if getattr(sys, "frozen", False):
    # 打包后，资源文件在 sys._MEIPASS 目录下
    BASE_DIR = Path(sys._MEIPASS)  # type: ignore
else:
    BASE_DIR = Path(__file__).parent

# ── 创建 Flask 应用 ──────────────────────────────
from flask import Flask, send_from_directory

TEMPLATE_DIR = BASE_DIR / "web" / "templates"
STATIC_DIR   = BASE_DIR / "web" / "static"

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR),
)
app.config["JSON_AS_ASCII"] = False         # 确保中文 JSON 不转义
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0 # 开发时禁用缓存

# ── 注册 Blueprint ───────────────────────────────
from web.routes import bp as api_bp
app.register_blueprint(api_bp)

# ── 前端入口路由 ─────────────────────────────────
@app.get("/")
def index():
    return send_from_directory(str(TEMPLATE_DIR), "index.html")


# ── 启动入口 ─────────────────────────────────────
def main() -> None:
    from config import get_config
    cfg  = get_config()
    host = cfg.get("app", "host") or "127.0.0.1"
    port = int(cfg.get("app", "port") or 5000)
    url  = f"http://{host}:{port}"

    logger.info("=" * 50)
    logger.info("  微信群聊 AI 总结 启动中...")
    logger.info("  访问地址：%s", url)
    logger.info("=" * 50)

    # 启动后台定时任务
    from core.scheduler import start_scheduler
    start_scheduler()

    # 延迟 1.2s 后自动打开浏览器（等 Flask 启动）
    def _open_browser():
        import time
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    app.run(
        host=host,
        port=port,
        debug=cfg.get("app", "debug") or False,
        use_reloader=False,   # 避免双进程时打开两次浏览器
    )


if __name__ == "__main__":
    main()
