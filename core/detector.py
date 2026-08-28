"""Detect Claude Desktop installation and current localization status.

Extracted from launcher_bridge.py so the GUI can query state without spawning
a subprocess. All functions are pure queries; the GUI/installer layer performs
the actual patch/restore.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BACKUP_ROOT = Path(os.environ.get("LOCALAPPDATA", "")) / "Claude-zh-CN-official-backup"
PATCH_STATE_PATH = BACKUP_ROOT / "json-only" / "patch-state.json"
# Store 版 Claude 的包族名（AppX 启动兜底用）；权威值优先取 Get-AppxPackage 动态结果
APPX_PFN = "Claude_pzs8sxrjxfjjc!Claude"
# —— 自动定位 Claude 真正读取的主配置文件(不写死路径) ——
CONFIG_CANDIDATE_DIRS = ("Claude-3p", "AnthropicClaude", "Claude", "claude-3p", "claude")
MAIN_CONFIG_MARKERS = (
    "oauth:tokenCache",
    "lastKnownAccountUuid",
    "updaterLastSeenVersion",
    "windowSizeWasSignedIn",
    "bootFrameLayout",
    "quickWindowPosition",
)


@functools.lru_cache(maxsize=1)
def resolve_claude_config() -> Path:
    """自动扫描定位 Claude 真正读取的主配置文件,避免写死路径。

    在 Local/Roaming × 常见目录名 里找 config.json,按"是否含 Claude 主配置
    独有字段"打分,选出分数最高、最近修改的那个。Claude 升级、换安装方式时
    会自动跟随,不再依赖固定的猜测路径。
    """
    candidates: list[tuple[int, float, Path]] = []
    for base in (Path(os.environ.get("LOCALAPPDATA", "")), Path(os.environ.get("APPDATA", ""))):
        for name in CONFIG_CANDIDATE_DIRS:
            p = base / name / "config.json"
            if not p.is_file():
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                mtime = p.stat().st_mtime
            except OSError:
                continue
            score = sum(1 for m in MAIN_CONFIG_MARKERS if m in content)
            candidates.append((score, mtime, p))
    if not candidates:
        # 兜底:一个都扫不到时,默认写到 Claude Desktop 常用的 Local\Claude-3p
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Claude-3p" / "config.json"
    score, mtime, path = max(candidates, key=lambda c: (c[0], c[1]))
    return path


CONFIG_PATH = resolve_claude_config()


def run_capture(command: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        cwd=str(Path.cwd()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        timeout=timeout,
        shell=False,
        creationflags=CREATE_NO_WINDOW,
    )


def python_exe() -> str:
    return sys.executable


def _appx_registered_package() -> tuple[Path, str] | None:
    """查询当前用户注册激活的 Claude Store 包。

    Get-AppxPackage 只返回当前用户实际注册可启动的包：天然排除 WindowsApps
    里残留的未激活旧版本目录（按版本号挑最高会选错包），且不依赖受控目录的
    列目录权限（WindowsApps 禁止普通进程列目录，glob 恒为空）。
    返回 (包根目录, 包族名 PackageFamilyName)。
    """
    try:
        result = run_capture(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-AppxPackage -Name 'Claude' | Select-Object -First 1 | "
                    "ForEach-Object { $_.InstallLocation + '|' + $_.PackageFamilyName }"
                ),
            ],
            timeout=10,
        )
    except Exception:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        location, _, family = line.rpartition("|")
        location = location.strip()
        if location:
            return Path(location), (family.strip() or APPX_PFN)
    return None


def find_windowsapps_packages() -> list[Path]:
    # 权威来源：当前用户注册激活的包
    registered = _appx_registered_package()
    if registered:
        location, _family = registered
        for candidate in (location / "app", location):
            if (candidate / "resources" / "en-US.json").is_file():
                return [candidate]
    # 兜底：直接列目录（需管理员权限；WindowsApps 禁止普通进程列目录，glob 恒空）
    base = Path(r"C:\Program Files\WindowsApps")
    if not base.exists():
        return []
    packages = sorted(
        {
            path.parent.parent
            for path in base.glob("Claude_*_x64__*/app/resources/en-US.json")
            if path.is_file()
        },
        key=version_key,
        reverse=True,
    )
    return packages


def find_appdata_packages() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    anthropic = Path(local) / "AnthropicClaude"
    if not anthropic.exists():
        return []
    candidates = []
    for resource in [
        anthropic / "resources" / "en-US.json",
        anthropic / "app" / "resources" / "en-US.json",
        *anthropic.glob("app*/resources/en-US.json"),
    ]:
        if resource.is_file():
            candidates.append(resource.parent.parent)
    return sorted(
        set(candidates),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )


def version_key(app_dir: Path) -> tuple[int, ...]:
    name = app_dir.parent.name if app_dir.name.lower() == "app" else app_dir.name
    parts = name.split("_")
    if len(parts) < 2:
        return ()
    values: list[int] = []
    for part in parts[1].split("."):
        try:
            values.append(int(part))
        except ValueError:
            values.append(0)
    return tuple(values)


def version_label(app_dir: Path) -> str:
    key = version_key(app_dir)
    if key:
        return ".".join(str(part) for part in key)
    package = app_dir.parent.name if app_dir.name.lower() == "app" else app_dir.name
    match = re.search(r"(\d+(?:\.\d+){1,3})", package)
    return match.group(1) if match else "unknown"


def resolve_app_dir(target: str | None = None, app_dir: str | None = None) -> Path | None:
    if app_dir:
        path = Path(app_dir)
        if (path / "resources" / "en-US.json").is_file():
            return path
        if (path / "app" / "resources" / "en-US.json").is_file():
            return path / "app"
        return None

    windows = find_windowsapps_packages()
    appdata = find_appdata_packages()
    choice = (target or "auto").lower()
    if choice in {"windows-apps", "windowsapps"}:
        return windows[0] if windows else None
    if choice in {"app-data", "appdata"}:
        return appdata[0] if appdata else None
    if choice == "manual":
        return None

    # "auto": prefer the running Claude process, then store, then user edition
    try:
        result = run_capture(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "$p = Get-Process -Name claude -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.Path } | Select-Object -First 1; "
                    "if ($p) { Split-Path -Parent $p.Path }"
                ),
            ],
            timeout=6,
        )
        for line in result.stdout.splitlines():
            parent = Path(line.strip())
            if (parent / "resources" / "en-US.json").is_file():
                return parent
            if (parent / "app" / "resources" / "en-US.json").is_file():
                return parent / "app"
    except Exception:
        pass

    if windows:
        return windows[0]
    if appdata:
        return appdata[0]
    return None


def has_backup() -> bool:
    if not BACKUP_ROOT.exists():
        return False
    return any(BACKUP_ROOT.rglob("*"))


def locale_is_zh() -> bool:
    if not CONFIG_PATH.is_file():
        return False
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("locale") == "zh-CN"


# patch_chunks 注入的运行时标记：官方 bundle 绝不包含，是“已打补丁”的可靠指纹。
# 不能用“bundle 文本含 zh-CN”判断——官方语言列表数组天然含 zh-CN，必然误报。
CHUNK_PATCH_MARKERS = (
    "__CLAUDE_ZH_CN_FONT_PATCH_BEGIN__",
    "__CLAUDE_ZH_CN_SESSION_DELETE_PATCH_BEGIN__",
)


def file_has_chunk_marker(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(marker in text for marker in CHUNK_PATCH_MARKERS)


def same_app_dir(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def whitelist_has_zh(app_dir: Path) -> bool:
    """判断当前安装目录是否已打中文补丁。

    依据补丁指纹：patch-state 记录匹配当前目录，或 bundle 里有 chunk
    运行时标记（官方文件绝不含）。
    """
    resources = app_dir / "resources"
    assets = resources / "ion-dist" / "assets"
    if not assets.exists():
        return False

    if PATCH_STATE_PATH.is_file():
        try:
            state = json.loads(PATCH_STATE_PATH.read_text(encoding="utf-8"))
            state_app_dir = Path(state.get("app_dir", ""))
            whitelist_files = state.get("whitelist_files", [])
            if same_app_dir(state_app_dir, app_dir) and isinstance(whitelist_files, list):
                if any(
                    isinstance(relative, str) and (resources / relative).is_file()
                    for relative in whitelist_files
                ):
                    return True
        except (OSError, ValueError, TypeError):
            pass

    for path in assets.rglob("index-*.js"):
        if file_has_chunk_marker(path):
            return True
    return False


def zh_resources_present(app_dir: Path) -> bool:
    resources = app_dir / "resources"
    targets = [
        resources / "zh-CN.json",
        resources / "ion-dist" / "i18n" / "zh-CN.json",
        resources / "ion-dist" / "i18n" / "statsig" / "zh-CN.json",
    ]
    return all(path.is_file() for path in targets)


def python_available() -> bool:
    try:
        result = run_capture([python_exe(), "--version"], timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def powershell_available() -> bool:
    return shutil.which("powershell") is not None


def build_status(target: str | None = None, app_dir: str | None = None) -> dict:
    resolved = resolve_app_dir(target=target, app_dir=app_dir)
    installed = resolved is not None
    localized = False
    language = "未安装"
    version = "unknown"
    install_path = "未找到 Claude Desktop"
    message = "未找到 Claude Desktop 安装。"
    checks = [
        {"label": "Claude Desktop", "value": "未找到", "tone": "danger"},
        {"label": "中文资源", "value": "未知", "tone": "warn"},
        {"label": "语言白名单", "value": "未知", "tone": "warn"},
        {
            "label": "备份文件",
            "value": "待生成" if not has_backup() else "已准备",
            "tone": "good" if has_backup() else "warn",
        },
        {
            "label": "Python / PowerShell",
            "value": "可用" if python_available() and powershell_available() else "缺失",
            "tone": "good" if python_available() and powershell_available() else "danger",
        },
    ]

    state = "missing"
    selected_target = target or "auto"
    if resolved:
        install_path = str(resolved)
        version = version_label(resolved)
        zh_files = zh_resources_present(resolved)
        whitelist = whitelist_has_zh(resolved)
        locale = locale_is_zh()
        backup = has_backup()
        localized = zh_files and whitelist
        language = "zh-CN" if locale or localized else "未安装"
        checks[0] = {"label": "Claude Desktop", "value": "已安装", "tone": "good"}
        checks[1] = {
            "label": "中文资源",
            "value": "已写入" if zh_files else "未安装",
            "tone": "good" if zh_files else "warn",
        }
        checks[2] = {
            "label": "语言白名单",
            "value": "包含 zh-CN" if whitelist else "待写入",
            "tone": "good" if whitelist else "warn",
        }
        checks[3] = {
            "label": "备份文件",
            "value": "已准备" if backup else "待生成",
            "tone": "good" if backup else "warn",
        }
        if localized:
            state = "ready"
            message = "Claude Desktop 中文版可以打开。"
        else:
            state = "repair"
            message = "Claude Desktop 已找到，可以安装中文补丁。"

        if "WindowsApps" in install_path:
            selected_target = "windows-apps" if selected_target == "auto" else selected_target
        elif "AnthropicClaude" in install_path:
            selected_target = "app-data" if selected_target == "auto" else selected_target

    return {
        "state": state,
        "installed": installed,
        "localized": localized,
        "version": version,
        "language": language,
        "target": selected_target,
        "installPath": install_path,
        "message": message,
        "checks": checks,
        "appDir": str(resolved) if resolved else None,
    }


def stop_claude() -> None:
    try:
        run_capture(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-Process -Name claude -ErrorAction SilentlyContinue | "
                    "Stop-Process -Force -ErrorAction SilentlyContinue"
                ),
            ],
            timeout=15,
        )
        time.sleep(1.5)
    except Exception:
        pass


def _appx_candidates() -> list[str]:
    """Store 版启动 URI 候选：注册包族名优先，Get-StartApps 动态补充，硬编码兜底。

    Anthropic 换包签名后硬编码 PFN 会失效，动态查询可自动跟随。
    """
    uris: list[str] = []
    registered = _appx_registered_package()
    if registered:
        uris.append(f"shell:AppsFolder\\{registered[1]}")
    if f"shell:AppsFolder\\{APPX_PFN}" not in uris:
        uris.append(f"shell:AppsFolder\\{APPX_PFN}")
    try:
        result = run_capture(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-StartApps | "
                    "Where-Object { $_.AppID -like 'Claude_*' -or $_.AppID -like 'AnthropicClaude*' } | "
                    "Select-Object -ExpandProperty AppID"
                ),
            ],
            timeout=10,
        )
    except Exception:
        return uris
    for line in (result.stdout or "").splitlines():
        app_id = line.strip()
        if app_id and f"shell:AppsFolder\\{app_id}" not in uris:
            uris.append(f"shell:AppsFolder\\{app_id}")
    return uris


def open_claude(app_dir) -> tuple[bool, str]:
    if app_dir is not None and not isinstance(app_dir, Path):
        app_dir = Path(app_dir)
    candidates: list[Path] = []
    if app_dir:
        parent = app_dir.parent if app_dir.name.lower() == "app" else app_dir
        candidates.extend(
            [
                app_dir / "Claude.exe",
                parent / "Claude.exe",
                parent / "app" / "Claude.exe",
            ]
        )
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "AnthropicClaude" / "Claude.exe")

    first_error: str | None = None
    for path in candidates:
        if not path.is_file():
            continue
        try:
            subprocess.Popen([str(path)], cwd=str(path.parent))
            return True, f"已启动: {path}"
        except OSError as exc:
            # WindowsApps 等受控目录禁止直接 CreateProcess（WinError 5）；
            # 记录原因后继续尝试其余候选和 shell 协议，而不是直接放弃。
            if first_error is None:
                first_error = str(exc)
            continue

    # Store 版 / 受控目录：经 shell:AppsFolder 协议交给 shell 启动
    appx_uris = _appx_candidates()
    for uri in appx_uris:
        try:
            result = run_capture(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Start-Process '{uri}'",
                ],
                timeout=10,
            )
            if result.returncode == 0:
                return True, "已通过 AppX 协议启动 Claude Desktop。"
        except Exception:
            continue
    # 最后兜底：仅当动态查询确认 Store 包真实存在时才交给 explorer（用户会话
    # 权限启动）；否则包不存在时 explorer 仍返回成功，会把"未安装"误报成已启动。
    if len(appx_uris) > 1:
        try:
            subprocess.Popen(["explorer.exe", appx_uris[1]])
            return True, "已通过 explorer 启动 Claude Desktop。"
        except OSError:
            pass

    if first_error:
        return False, f"启动失败: {first_error}"
    return False, "未找到 Claude.exe，无法打开。"
