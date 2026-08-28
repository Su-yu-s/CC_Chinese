from __future__ import annotations

import ctypes
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


def is_windowsapps_path(path: Path) -> bool:
    if os.name != "nt":
        return False

    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        candidate = os.path.normcase(str(path.resolve(strict=False)))
        windowsapps = os.path.normcase(str(root.resolve(strict=False)))
        return os.path.commonpath([candidate, windowsapps]) == windowsapps
    except ValueError:
        return False


def is_windows_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(script_path: Path, script_args: Sequence[str]) -> bool:
    if os.name != "nt":
        return False

    parameters = subprocess.list2cmdline([str(script_path), *script_args])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            sys.executable,
            parameters,
            str(script_path.parent),
            0,
        )
    except (AttributeError, OSError):
        return False
    return int(result) > 32


def ensure_admin_for_windowsapps(
    app_dir: Path,
    script_path: Path,
    script_args: Sequence[str],
) -> int | None:
    if not is_windowsapps_path(app_dir) or is_windows_admin():
        return None

    print()
    print("[!] 检测到 WindowsApps 安装目录，正在请求管理员权限...")
    if relaunch_as_admin(script_path, script_args):
        print("[OK] 已启动管理员进程，当前进程即将退出。")
        return 0

    print("[X] 未获得管理员权限，补丁未执行。")
    print("请右键 PowerShell 或 Windows Terminal，选择“以管理员身份运行”，然后重新执行。")
    return 1


def print_permission_denied_hint(path: Path) -> None:
    print()
    print("[权限不足] 无法写入 Claude 安装文件：")
    print(f"  {path}")
    print("可能原因：")
    print("  1) 当前终端没有管理员权限（WindowsApps 目录写入需要）")
    print("  2) Claude 仍在运行，文件被占用")
    print("处理步骤：")
    print("  1. 完全关闭 Claude（托盘图标也退出）")
    print("  2. 右键 PowerShell / Windows Terminal -> 以管理员身份运行")
    print("  3. 重新执行 claude-zh-cn.ps1 或对应安装脚本")
    if not is_windows_admin():
        print("当前状态：未检测到管理员权限")
    else:
        print("当前状态：已是管理员，请优先检查 Claude 是否仍在运行")


def copy2_best_effort(src: Path, dst: Path, *, context: str) -> bool:
    """Copy a file and retry once after clearing the destination readonly bit."""
    try:
        shutil.copy2(src, dst)
        return True
    except PermissionError:
        if dst.exists():
            try:
                dst.chmod(dst.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
        try:
            shutil.copy2(src, dst)
            return True
        except OSError as e:
            print(f"Warning: cannot copy {context} from {src} to {dst}: {e}; skipping")
            return False
    except OSError as e:
        print(f"Warning: cannot copy {context} from {src} to {dst}: {e}; skipping")
        return False


def write_text_best_effort(path: Path, text: str, *, context: str) -> bool:
    """Write text and degrade gracefully on Windows permission issues."""
    try:
        path.write_text(text, encoding="utf-8")
        return True
    except PermissionError:
        try:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass
        try:
            path.write_text(text, encoding="utf-8")
            return True
        except OSError as e:
            print(f"Warning: cannot write {context} at {path}: {e}; skipping")
            return False
    except OSError as e:
        print(f"Warning: cannot write {context} at {path}: {e}; skipping")
        return False


def ensure_windowsapps_writable(app_dir: Path) -> bool:
    """对 WindowsApps 下的 app\\resources 取所有权并授权 Administrators 完全控制。

    商店版 Claude 目录受 ACL 保护（owner 是 TrustedInstaller/SYSTEM），管理员也常常
    写不进去 —— 仅靠 copy2_best_effort 的 chmod 清只读位无效。需 takeown + icacls。
    仅在「是 WindowsApps 路径」且「当前是管理员」时执行；非 WindowsApps（如用户版、
    测试临时目录）或非管理员时直接跳过，不影响原流程。返回是否拿到写入权限。
    """
    if os.name != "nt" or not is_windowsapps_path(app_dir) or not is_windows_admin():
        return True
    target = app_dir / "resources"
    if not target.exists():
        return True
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        own = subprocess.run(
            ["takeown", "/f", str(target), "/r", "/d", "y"],
            capture_output=True, text=True, timeout=120,
            creationflags=flags,
        )
        grant = subprocess.run(
            ["icacls", str(target), "/grant", "*S-1-5-32-544:(OI)(CI)F", "/t", "/c", "/q"],
            capture_output=True, text=True, timeout=120,
            creationflags=flags,
        )
        return own.returncode == 0 and grant.returncode == 0
    except Exception:
        return False
