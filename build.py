"""用 PyInstaller 打包为 onedir 目录模式，并在启动时请求管理员权限。

用法(用装有 PySide6 的 Python 3.13 环境):
    python -m pip install -r requirements.txt pyinstaller
    python build.py
产物: dist/CC_Chinese/CC_Chinese.exe（双击后立即请求管理员权限）

--upx-dir 指向 scripts/upx/UPX(若未装可跑 scripts/install_upx.py 下载)。
"""
import os
import sys
from pathlib import Path


def _sanitize_build_path() -> None:
    """Limit PyInstaller DLL discovery to Python and Windows system paths."""
    python_dir = Path(sys.executable).resolve().parent
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    clean_entries = [
        python_dir,
        python_dir / "Scripts",
        system_root / "System32",
        system_root,
        system_root / "System32" / "Wbem",
    ]
    os.environ["PATH"] = os.pathsep.join(str(path) for path in clean_entries)
    print("Using isolated Python/Windows PATH for a clean build")


_sanitize_build_path()

import PyInstaller.__main__

UPX_DIR = Path(__file__).resolve().parent / "scripts" / "upx"
if not UPX_DIR.exists():
    print(f"Warning: UPX dir not found at {UPX_DIR} — 跳过 UPX 压缩(包体会较大)")
    upx_dir_arg: list[str] = []
else:
    upx_dir_arg = ["--upx-dir", str(UPX_DIR)]
    # 硬件加速 DLL 不能 UPX(会崩溃),明确排除
    upx_dir_arg += [
        "--upx-exclude=libGLESv2.dll",
        "--upx-exclude=libEGL.dll",
        "--upx-exclude=d3dcompiler_47.dll",
        "--upx-exclude=QtWebEngineProcess.exe",
        "--upx-exclude=_uuid.pyd",
        "--upx-exclude=python3.dll",
    ]

PyInstaller.__main__.run([
    "main.py",
    "--windowed",
    "--name=CC_Chinese",
    "--icon=assets/icon.ico",
    "--uac-admin",
    # Python modules are already collected into PYZ through --paths=core.
    # Only runtime JSON assets must remain as external data; copying all of
    # core/ would also ship stale __pycache__ files from the build machine.
    "--add-data=core/resources;core/resources",
    "--add-data=assets;assets",
    "--paths=core",          # 让 PyInstaller 解析 core/ 里的顶层 import
    "--clean",
    "--noconfirm",
    *upx_dir_arg,
])

# Refresh only this rebuilt executable's shell item (no global icon-cache purge).
if sys.platform == "win32":
    import ctypes
    output_exe = Path(__file__).resolve().parent / "dist" / "CC_Chinese" / "CC_Chinese.exe"
    ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005, ctypes.c_wchar_p(str(output_exe)), None)
