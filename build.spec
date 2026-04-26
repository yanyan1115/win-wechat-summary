# build.spec — PyInstaller 打包配置
# 使用方法：
#   d:\APP\win-wechat-summary\.venv\Scripts\pyinstaller.exe build.spec
#
# 说明：
#   --onefile  模式打包为单个 exe（体积较大但分发方便）
#   --windowed 隐藏控制台窗口（由 pystray 托盘接管）
#   datas      需要随 exe 一起打包的静态资源

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ── 收集数据文件 ────────────────────────────────────────────────
datas = [
    # Flask 模板和静态资源
    ('web/templates',               'web/templates'),
    ('web/static',                  'web/static'),
    # 图标
    ('resources/icon.ico',          'resources'),
    # PyWxDump 偏移量数据库
    ('.venv/Lib/site-packages/pywxdump/WX_OFFS.json', 'pywxdump'),
]

# ── 隐式导入（PyInstaller 扫不到的动态导入）──────────────────────
hiddenimports = [
    # Flask / Werkzeug 内部
    'flask',
    'werkzeug',
    'werkzeug.routing',
    'werkzeug.serving',
    'jinja2',
    'click',
    # 本项目 ai 子包（PyInstaller 扫不到动态导入）
    'ai',
    'ai.base',
    'ai.factory',
    'ai.claude_provider',
    'ai.openai_provider',
    'ai.ollama_provider',
    # 本项目 core 子包
    'core',
    'core.wechat',
    'core.history',
    'core.scheduler',
    # AI SDK
    'anthropic',
    'openai',
    'requests',
    # APScheduler
    'apscheduler',
    'apscheduler.schedulers.background',
    'apscheduler.triggers.cron',
    'apscheduler.executors.pool',
    # 系统托盘
    'pystray',
    'pystray._win32',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
    # 其他
    'sqlite3',
    'cryptography',
    'ctypes',
    'ctypes.wintypes',
    'concurrent.futures',
    'asyncio',
    'threading',
    # pywxdump 子模块
    'pywxdump',
    'pywxdump.wx_core',
    'pywxdump.wx_core.util',
    'pywxdump.db',
]

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 不需要的大型库，减小体积
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'IPython',
        'jupyter',
        'notebook',
        'pytest',
        'test',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='WeChat-Summary',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,         # 尝试用 UPX 压缩（需要系统安装 UPX，没有也不报错）
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,    # --windowed：不显示控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='resources/icon.ico',
    version_file=None,
)
