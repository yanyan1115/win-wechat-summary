"""
app.py — 应用主入口

启动 Flask + 系统托盘图标。
关闭浏览器后程序继续在托盘运行，右键菜单可打开网页或完全退出。
支持 PyInstaller 打包后运行（frozen 模式）。
"""

import logging
import os
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
app.config["JSON_AS_ASCII"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ── 注册 Blueprint ───────────────────────────────
from web.routes import bp as api_bp
app.register_blueprint(api_bp)

# ── 前端入口路由 ─────────────────────────────────
@app.get("/")
def index():
    return send_from_directory(str(TEMPLATE_DIR), "index.html")


# ── 托盘图标 ─────────────────────────────────────

def _make_tray_icon() -> "PIL.Image.Image":
    """
    生成托盘图标：深绿色圆形背景 + 白色「微」字。
    不依赖外部图片文件，打包时也能正常工作。
    """
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 背景圆
    draw.ellipse([2, 2, size - 2, size - 2], fill=(34, 139, 34, 255))
    # 文字「微」，用默认字体（不依赖系统字体文件）
    try:
        font = ImageFont.truetype("msyh.ttc", 32)   # 微软雅黑
    except Exception:
        try:
            font = ImageFont.truetype("simhei.ttf", 32)
        except Exception:
            font = ImageFont.load_default()
    text = "微"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
              text, font=font, fill=(255, 255, 255, 255))
    return img


def _run_tray(url: str) -> None:
    """在主线程运行系统托盘（pystray 在 Windows 上必须跑在主线程）"""
    try:
        import pystray
        from pystray import MenuItem as Item, Menu
    except ImportError:
        logger.warning("pystray 未安装，跳过托盘图标（程序仍可正常使用）")
        # 没有托盘时，阻塞主线程防止进程退出
        threading.Event().wait()
        return

    icon_img = _make_tray_icon()

    def on_open(_icon, _item):
        webbrowser.open(url)

    def on_quit(_icon, _item):
        _icon.stop()
        os._exit(0)

    tray = pystray.Icon(
        name="wechat-summary",
        icon=icon_img,
        title="微信群聊 AI 总结",
        menu=Menu(
            Item("打开网页", on_open, default=True),   # 双击也触发
            Menu.SEPARATOR,
            Item("退出", on_quit),
        ),
    )
    logger.info("托盘图标已启动，关闭浏览器后程序继续在后台运行")
    tray.run()   # 阻塞，直到 on_quit 调用 stop()


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

    # Flask 在子线程运行（非阻塞）
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="flask",
    )
    flask_thread.start()

    # 延迟 1.2s 后自动打开浏览器
    def _open_browser():
        import time
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    # 主线程运行托盘（阻塞直到用户点"退出"）
    _run_tray(url)


if __name__ == "__main__":
    main()
