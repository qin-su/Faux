# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：faux-server.exe（onefile，控制台窗口）。

构建：在 deploy 目录执行 build.bat（或 python -m PyInstaller faux.spec）
"""
from PyInstaller.utils.hooks import collect_submodules

hidden = (
    collect_submodules("uvicorn")
    + [
        "httptools",       # uvicorn[standard] 高性能 HTTP 解析（动态导入）
        "websockets",      # uvicorn[standard] websocket 支持（动态导入）
        "pika",
        "dotenv",
    ]
)

a = Analysis(
    ["launcher.py"],
    pathex=[".."],                              # 使 app 包可被发现
    binaries=[],
    datas=[("../app/static", "app/static")],    # Web Dashboard（main.py 按 app/static 相对定位）
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas", "pytest",
        "watchfiles",                           # 仅 --reload 需要，打包模式不用
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="faux-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,          # 保留控制台：日志可见，Ctrl+C 停止
    disable_windowed_traceback=False,
)
