# -*- mode: python ; coding: utf-8 -*-
"""用于构建 cuaflow 独立目录发行包的 PyInstaller 配置文件：
- cuaflow-desktop(.exe)：图形界面启动器 + 本地服务端 + 后台调度器
- cuaflow(.exe)：        命令行工具（do、resume、serve、flows、schedules、stats、doctor、install-browser）
- cuaflow-tool(.exe)：   智能体命令行辅助工具
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

SPEC_DIR = Path(SPECPATH).resolve()
ROOT_DIR = SPEC_DIR.parent
SRC_DIR = ROOT_DIR / "src"
ICO_PATH = SPEC_DIR / "cuaflow.ico"
ICON_ARG = str(ICO_PATH) if ICO_PATH.is_file() else None

datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
hiddenimports: list[str] = []

# 打包 cuaflow 提示词模板、默认配置以及编译后的 Web 控制台静态资源
datas += collect_data_files("cuaflow")
if ICO_PATH.is_file():
    datas.append((str(ICO_PATH), "."))

# 打包 Playwright Python 客户端与 Node CDP 驱动（体积轻量；除非显式指定，否则不包含浏览器本体）
pw_datas, pw_bins, pw_hidden = collect_all("playwright")
datas += pw_datas
binaries += pw_bins
hiddenimports += pw_hidden

for pkg_meta in ("cuaflow", "playwright", "fastapi", "uvicorn", "pydantic", "httpx", "typer", "rich"):
    try:
        datas += copy_metadata(pkg_meta)
    except Exception:
        pass

# 当 CUAFLOW_BUNDLE_CHROMIUM=1 时，可选打包完整离线版 Chromium 浏览器
if os.environ.get("CUAFLOW_BUNDLE_CHROMIUM", "").strip().lower() in ("1", "true", "yes"):
    pw_browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if pw_browsers and Path(pw_browsers).is_dir():
        datas.append((pw_browsers, "ms-playwright"))

# 纳入全部 cuaflow 子模块、Uvicorn/FastAPI 动态导入项，以及 @flow.step 中常用的标准库与自动化模块
hiddenimports += collect_submodules("cuaflow")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "openpyxl",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "mss",
    "yaml",
    "dotenv",
    "multipart",
    # 录制的 Flow 脚本常会动态导入的标准库模块
    "csv",
    "sqlite3",
    "decimal",
    "statistics",
    "zipfile",
    "tarfile",
    "difflib",
    "html.parser",
    "xml.etree.ElementTree",
    "urllib.request",
    "urllib.parse",
    "tkinter",
]

if sys.platform == "win32":
    for win_pkg in ("pywinauto", "uiautomation", "pyautogui", "psutil", "win32api", "win32con", "win32gui", "win32process", "comtypes"):
        try:
            hiddenimports += collect_submodules(win_pkg)
        except Exception:
            hiddenimports.append(win_pkg)
    # 打包单窗口原生 WebView2 外壳（pywebview 及其底层 pythonnet/clr_loader 动态库）
    for wv_pkg in ("webview", "pythonnet", "clr_loader", "proxy_tools", "bottle"):
        try:
            wv_datas, wv_bins, wv_hidden = collect_all(wv_pkg)
            datas += wv_datas
            binaries += wv_bins
            hiddenimports += wv_hidden
        except Exception:
            pass

excludes = ["pytest", "_pytest", "IPython", "matplotlib", "scipy", "numpy.testing"]

common_kwargs = dict(
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

a_desktop = Analysis([str(SPEC_DIR / "entry_desktop.py")], **common_kwargs)
pyz_desktop = PYZ(a_desktop.pure)
exe_desktop = EXE(
    pyz_desktop,
    a_desktop.scripts,
    [],
    exclude_binaries=True,
    name="cuaflow-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON_ARG,
)

a_cli = Analysis([str(SPEC_DIR / "entry_cli.py")], **common_kwargs)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="cuaflow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON_ARG,
)

a_tool = Analysis([str(SPEC_DIR / "entry_tool.py")], **common_kwargs)
pyz_tool = PYZ(a_tool.pure)
exe_tool = EXE(
    pyz_tool,
    a_tool.scripts,
    [],
    exclude_binaries=True,
    name="cuaflow-tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=ICON_ARG,
)

coll = COLLECT(
    exe_desktop,
    a_desktop.binaries,
    a_desktop.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    exe_tool,
    a_tool.binaries,
    a_tool.datas,
    strip=False,
    upx=False,
    name="cuaflow",
)
