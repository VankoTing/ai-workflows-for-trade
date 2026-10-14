# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 把桌面版打成单文件 exe。

打包：
    pyinstaller --clean desktop.spec

产物：
    dist/阀门询价整理.exe
"""

datas = [
    ("src", "src"),
    ("web", "web"),
    ("glossary", "glossary"),
]

# uvicorn 是动态导入实现，必须显式声明，否则打包后启动会报错
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    "multipart",
    "python_multipart",
]

# 这些库用不到，排除掉能显著减小体积
excludes = [
    "tkinter", "unittest", "pydoc_data",
    "matplotlib", "numpy", "pandas", "scipy", "PIL",
    "PyQt5", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook",
]

a = Analysis(
    ["desktop.py"],
    # 必须把 src 加进搜索路径，否则 PyInstaller 找不到 pipeline.py，
    # 连带它的依赖（openpyxl / python-docx / pypdf）都不会被打包
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="阀门询价整理",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
