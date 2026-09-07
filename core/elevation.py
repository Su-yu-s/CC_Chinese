"""Restricted Windows elevation broker for protected Claude installations."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from best_effort_io import is_windows_admin, is_windowsapps_path
from safe_io import atomic_write_text


def _project_root() -> Path:
    """Return the project root directory (parent of core/).

    When launched as an elevated subprocess the cwd is the exe directory,
    so we must resolve the project root from the module file location.
    """
    return Path(__file__).resolve().parent.parent


ALLOWED_ACTIONS = {"patch", "restore"}
NONCE_RE = re.compile(r"^[a-f0-9]{32}$")
ADMIN_SIDS = {"S-1-5-18", "S-1-5-32-544"}
WRITE_RIGHTS = {
    "FullControl",
    "Modify",
    "Write",
    "WriteData",
    "CreateFiles",
    "CreateDirectories",
    "Delete",
    "ChangePermissions",
    "TakeOwnership",
}


def new_nonce() -> str:
    return uuid.uuid4().hex


def elevation_result_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return base / "CC_Chinese" / "elevation-results"


def result_path(nonce: str) -> Path:
    if not NONCE_RE.fullmatch(nonce):
        raise ValueError("invalid elevation nonce")
    return elevation_result_dir() / f"{nonce}.json"


def _has_reparse_component(path: Path) -> bool:
    flag = getattr(stat_module(), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    # Do not resolve first: resolving would follow the very reparse point this
    # check is meant to detect.
    current = Path(os.path.abspath(path))
    while current.parent != current:
        try:
            info = current.lstat()
        except OSError:
            # 权限不足时无法判断 reparse 状态，保守返回 False（调用方另有路径校验兜底）。
            # 只把 symlink 视为可疑：symlink 本身是人为构造的路径逃逸，permission error 是
            # 正常的系统保护行为，二者性质不同。
            try:
                return current.is_symlink()
            except OSError:
                return False
        if current.is_symlink() or (getattr(info, "st_file_attributes", 0) & flag):
            return True
        current = current.parent
    return False


def stat_module():
    # Kept behind a function so tests can monkeypatch file attribute constants.
    import stat

    return stat


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def protected_distribution(executable: Path | None = None) -> bool:
    """Only a non-reparse Program Files release may request administrator."""
    if os.name != "nt":
        return False
    exe = Path(executable or sys.executable)
    roots = [
        Path(value)
        for key in ("ProgramFiles", "ProgramFiles(x86)")
        if (value := os.environ.get(key))
    ]
    if not exe.is_file() or not any(_within(exe, root) for root in roots):
        return False
    if _has_reparse_component(exe):
        return False
    # Check the directory ACL rather than os.access(): an elevated token can
    # write Program Files by design, which must not make the trusted release
    # reject itself after UAC approval.
    acl = _acl_snapshot(exe.parent)
    if not acl:
        return False
    rules = acl.get("rules") or []
    if isinstance(rules, dict):
        rules = [rules]
    for rule in rules:
        if str(rule.get("type")) != "Allow":
            continue
        sid = str(rule.get("sid", ""))
        rights = {item.strip() for item in str(rule.get("rights", "")).split(",")}
        if sid not in ADMIN_SIDS and rights & WRITE_RIGHTS:
            return False
    return True


def validated_windowsapps_target(app_dir: Path) -> Path | None:
    raw_target = Path(os.path.abspath(app_dir))
    if _has_reparse_component(raw_target):
        return None
    target = raw_target.resolve(strict=False)
    if not is_windowsapps_path(target):
        return None
    if not (target / "resources" / "en-US.json").is_file():
        return None
    return target


def programdata_snapshot_root() -> Path:
    base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return base / "Claude-zh-CN-official-backup" / "snapshots"


def _acl_snapshot(path: Path) -> dict[str, Any] | None:
    if os.name != "nt":
        return {"owner": "S-1-5-32-544", "rules": []}
    # 内联路径到脚本，避免 PowerShell -Command + $args 的参数绑定问题
    escaped = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; Import-Module Microsoft.PowerShell.Security; "
        f"$a = Get-Acl -LiteralPath '{escaped}'; "
        f"$o = ([System.Security.Principal.NTAccount]$a.Owner).Translate([System.Security.Principal.SecurityIdentifier]).Value; "
        f"$r = @($a.Access | ForEach-Object {{ "
        f"[pscustomobject]@{{sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value; "
        f"type=$_.AccessControlType.ToString(); rights=$_.FileSystemRights.ToString()}} "
        f"}}); "
        f"[pscustomobject]@{{owner=$o; rules=$r}} | ConvertTo-Json -Depth 4 -Compress"
    )
    env = os.environ.copy()
    system_root = Path(env.get("SystemRoot", r"C:\Windows"))
    program_files = Path(env.get("ProgramFiles", r"C:\Program Files"))
    env["PSModulePath"] = os.pathsep.join(
        (
            str(system_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"),
            str(program_files / "WindowsPowerShell" / "Modules"),
        )
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=env,
        )
        return json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def _acl_has_safe_write_boundary(acl: dict[str, Any] | None) -> bool:
    """Return whether only administrative principals can write this ACL."""
    if not acl:
        return False
    rules = acl.get("rules") or []
    if isinstance(rules, dict):
        rules = [rules]
    admin_can_write = False
    for rule in rules:
        sid = str(rule.get("sid", ""))
        if str(rule.get("type")) != "Allow":
            continue
        rights = {item.strip() for item in str(rule.get("rights", "")).split(",")}
        if not rights & WRITE_RIGHTS:
            continue
        if sid in ADMIN_SIDS:
            admin_can_write = True
        else:
            # A normal-user write ACE makes snapshot baselines forgeable.
            return False
    return admin_can_write


def validate_protected_snapshot_root(root: Path | None = None) -> bool:
    """Validate that the snapshot root is safe for storing baseline data.

    The owner must be SYSTEM or BUILTIN\\Administrators, an administrative SID
    must retain write access, and no ordinary principal may have write access.
    A missing directory or reparse point is rejected.
    """
    root = Path(root or programdata_snapshot_root())
    if root.exists() and not root.is_dir():
        return False
    if _has_reparse_component(root):
        return False
    if not root.exists():
        return False
    acl = _acl_snapshot(root)
    if not acl:
        return False
    owner = str(acl.get("owner", ""))
    return owner in ADMIN_SIDS and _acl_has_safe_write_boundary(acl)


def _apply_protected_acl(path: Path) -> bool:
    """Set the canonical owner and ACL on one known-safe directory."""
    if os.name != "nt":
        return True
    try:
        owner_result = subprocess.run(
            [
                "icacls",
                str(path),
                "/setowner",
                "*S-1-5-32-544",
                "/q",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if owner_result.returncode != 0:
            return False
        acl_result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                "*S-1-5-18:(OI)(CI)F",
                "*S-1-5-32-544:(OI)(CI)F",
                "*S-1-5-32-545:(OI)(CI)RX",
                "/q",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return acl_result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _harden_existing_protected_directory(path: Path) -> bool:
    """Migrate a legacy tool directory without accepting an unsafe ACL.

    Older releases applied the correct write boundary but left the creating
    account as owner.  Such a directory may be re-owned only when it is a real
    directory, contains no reparse component, and already grants write access
    exclusively to administrative principals.
    """
    path = Path(path)
    if not path.is_dir() or _has_reparse_component(path):
        return False
    if not _acl_has_safe_write_boundary(_acl_snapshot(path)):
        return False
    return _apply_protected_acl(path)


def _create_protected_directory(path: Path) -> bool:
    """Create one directory and immediately replace inherited ACLs.

    This is only used by an already elevated process.  It never recurses and
    removes a just-created empty directory again when ACL hardening fails.
    """
    path = Path(path)
    if path.exists() or _has_reparse_component(path.parent):
        return False
    try:
        path.mkdir()
    except OSError:
        return False
    if _apply_protected_acl(path):
        return True
    try:
        path.rmdir()
    except OSError:
        pass
    return False


def ensure_protected_snapshot_root(root: Path | None = None) -> dict[str, Any]:
    """Create or verify the ProgramData snapshot root for portable releases."""
    root = Path(root or programdata_snapshot_root())
    base = root.parent
    if os.name != "nt" or not is_windows_admin():
        return {
            "success": False,
            "error_code": "PERMISSION_DENIED",
            "message": "CC_Chinese 需要管理员权限初始化安全快照目录。",
        }
    if _has_reparse_component(root):
        return {
            "success": False,
            "error_code": "SNAPSHOT_ROOT_INSECURE",
            "message": "安全快照目录包含重解析路径，已停止操作。",
        }

    created = False
    repaired = False
    for directory in (base, root):
        if directory.exists():
            if not directory.is_dir():
                return {
                    "success": False,
                    "error_code": "SNAPSHOT_ROOT_INSECURE",
                    "message": "现有安全快照目录权限不可信，未读取或覆盖其中内容。",
                }
            if not validate_protected_snapshot_root(directory):
                if (
                    not _harden_existing_protected_directory(directory)
                    or not validate_protected_snapshot_root(directory)
                ):
                    return {
                        "success": False,
                        "error_code": "SNAPSHOT_ROOT_INSECURE",
                        "message": "现有安全快照目录权限不可信，未读取或覆盖其中内容。",
                    }
                repaired = True
            continue
        if not _create_protected_directory(directory):
            return {
                "success": False,
                "error_code": "SNAPSHOT_ROOT_INIT_FAILED",
                "message": "无法创建并加固安全快照目录，未修改 Claude。",
            }
        created = True
        if not validate_protected_snapshot_root(directory):
            return {
                "success": False,
                "error_code": "SNAPSHOT_ROOT_INIT_FAILED",
                "message": "安全快照目录权限复检失败，未修改 Claude。",
            }

    return {"success": True, "created": created, "repaired": repaired, "root": str(root)}


def write_result(nonce: str, result: dict) -> Path:
    allowed = {
        "success": bool(result.get("success")),
        "state": str(result.get("state", "error"))[:32],
        "error_code": str(result.get("error_code", ""))[:64],
        "message": str(result.get("message", ""))[:500],
    }
    path = result_path(nonce)
    atomic_write_text(path, json.dumps(allowed, ensure_ascii=False) + "\n")
    return path


def read_result(nonce: str) -> dict | None:
    path = result_path(nonce)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return value if isinstance(value, dict) else None


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _is_dev_mode() -> bool:
    """Return True when running from a non-Program-Files Python install (dev mode).

    Production builds are expected to run from Program Files or a protected location;
    dev mode skips the strict protected_distribution gate so the broker can still
    function during development.
    """
    exe = Path(sys.executable)
    if not exe.is_file():
        return False
    for root_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(root_name)
        if root and _within(exe, Path(root)):
            return False
    return True


def launch_elevated_and_wait(action: str, app_dir: Path) -> dict:
    """Run one restricted elevated action; the caller must re-detect state."""
    if action not in ALLOWED_ACTIONS:
        return {"success": False, "error_code": "ELEVATION_TAMPERED", "message": "非法提权动作。"}
    if os.name != "nt":
        return {"success": False, "error_code": "ELEVATION_UNAVAILABLE", "message": "当前系统非 Windows。"}
    # 普通进程无法 lstat / 读取 WindowsApps（会 PermissionError），因此父进程只做
    # 纯路径前缀校验。reparse 点、en-US.json 存在性、快照根 ACL 等完整校验由提权
    # 后的子进程 run_elevated_action 执行（那时已是管理员，可正常访问）。
    target = Path(app_dir).resolve(strict=False)
    if not is_windowsapps_path(target):
        return {"success": False, "error_code": "ELEVATION_TAMPERED", "message": "Claude 目标未通过安全校验。"}
    nonce = new_nonce()
    # 使用 -m 模块启动方式，确保子进程能正确 import core/ 下的模块
    params = [
        "-m", "core.__main__",
        "--elevated-action", action,
        "--target-hint", str(target),
        "--nonce", nonce,
    ]
    params_str = subprocess.list2cmdline(params)
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(Path(sys.executable))
    info.lpParameters = params_str
    info.lpDirectory = str(_project_root())
    info.nShow = 0
    try:
        launched = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
        if not launched:
            return {"success": False, "error_code": "ELEVATION_CANCELLED", "message": "管理员授权已取消。"}
        ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        ctypes.windll.kernel32.CloseHandle(info.hProcess)
    except (AttributeError, OSError):
        return {"success": False, "error_code": "ELEVATION_UNAVAILABLE", "message": "无法启动安全提权进程。"}
    return read_result(nonce) or {
        "success": False,
        "error_code": "ELEVATION_TAMPERED",
        "message": "提权进程未返回有效结果。",
    }


def run_elevated_action(action: str, target_hint: str, nonce: str) -> int:
    """Entry used by the frozen executable after UAC approval."""
    if action not in ALLOWED_ACTIONS or not NONCE_RE.fullmatch(nonce):
        return 2
    if not is_windows_admin():
        return 3
    target = validated_windowsapps_target(Path(target_hint))
    if target is None:
        write_result(nonce, {"success": False, "error_code": "ELEVATION_TAMPERED", "message": "提权环境未通过完整性检查。"})
        return 4
    root_result = ensure_protected_snapshot_root()
    if not root_result.get("success"):
        write_result(nonce, root_result)
        return 4
    import installer

    result = (
        installer.run_install(target, elevated=True)
        if action == "patch"
        else installer.run_restore(target, elevated=True)
    )
    write_result(nonce, result)
    return 0 if result.get("success") else 1


__all__ = [
    "ALLOWED_ACTIONS",
    "ensure_protected_snapshot_root",
    "launch_elevated_and_wait",
    "new_nonce",
    "programdata_snapshot_root",
    "protected_distribution",
    "run_elevated_action",
    "validate_protected_snapshot_root",
    "validated_windowsapps_target",
]
