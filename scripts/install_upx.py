"""下载并解压 UPX(若系统 PATH 里找不到 upx.exe)。

用法:
    python scripts/install_upx.py
产物: scripts/upx/ 目录含 upx.exe(64-bit),供 build.py 通过 --upx-dir 使用。
"""
import os
import sys
import zipfile
import urllib.request
import shutil
from pathlib import Path

UPX_VERSION = "4.2.4"
# Windows x86_64 (AMD64) build
UPX_URL = f"https://github.com/upx/upx/releases/download/v{UPX_VERSION}/upx-{UPX_VERSION}-win64.zip"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
UPX_DIR = SCRIPTS_DIR / "upx"
UPX_EXE = UPX_DIR / "upx.exe"


def find_system_upx():
    """若系统 PATH 已有 upx.exe,直接返回路径。"""
    for p in (Path(os.environ.get("PATH", "")).parts
              if hasattr(Path(os.environ.get("PATH", "")), "parts")
              else []):
        candidate = Path(p) / "upx.exe"
        if candidate.exists():
            return str(candidate)
    # 常见位置兜底
    for cand in [
        r"C:\Program Files\upx\upx.exe",
        r"C:\Program Files (x86)\upx\upx.exe",
        r"C:\upx\upx.exe",
    ]:
        if os.path.exists(cand):
            return cand
    return None


def download_and_extract():
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        print(f"下载 UPX {UPX_VERSION}...")
        urllib.request.urlretrieve(UPX_URL, tmp.name)
        print(f"解压到 {UPX_DIR} ...")
        UPX_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp.name, "r") as z:
            # upx-4.2.4-win64/upx.exe 形式
            for name in z.namelist():
                if name.lower().endswith("upx.exe"):
                    dst = UPX_DIR / Path(name).name
                    with z.open(name) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
                    print(f"  -> {dst}")
        return str(UPX_EXE)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def main():
    found = find_system_upx()
    if found:
        print(f"UPX 已存在: {found}")
        return 0
    print("系统 PATH 未找到 upx.exe,开始下载...")
    exe = download_and_extract()
    print(f"UPX 已安装: {exe}")
    print(f"请确保 build.py 使用 --upx-dir {UPX_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
