"""Local, privacy-preserving diagnostics for the desktop UI.

The logger deliberately accepts only a small metadata allow-list.  Arbitrary
installer messages, tracebacks, environment values and absolute paths are not
written because they may contain chat text, credentials or workspace names.
"""
from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = 5

ERRORS: dict[str, tuple[str, str]] = {
    "RESTORE_APP_RUNNING": ("恢复", "Claude 仍在运行，请退出 Claude 后再恢复；尚未修改文件。"),
    "PATCH_RESOURCE_INVALID": ("资源检查", "工具内置翻译文件缺失或损坏，请使用完整的新版本目录；尚未修改 Claude。"),
    "LOCATE_NOT_FOUND": ("定位", "请先安装 Claude Desktop，或在设置中选择正确的本地安装目录。"),
    "LOCATE_AMBIGUOUS": ("定位", "检测到多个安装，请在设置中明确选择要处理的版本。"),
    "COMPAT_UNSUPPORTED": ("兼容", "此 Claude 版本暂不支持，请前往项目主页查看适配进度。"),
    "COMPAT_UNKNOWN": ("兼容", "无法确认当前安装是否兼容，已停止修改。"),
    "COMPAT_AMBIGUOUS": ("兼容", "资源同时匹配多个配置，已停止修改。"),
    "BASELINE_DIRTY": ("兼容", "检测到来源不明或不完整的修改，请先用 Claude 官方修复或重装。"),
    "SNAPSHOT_CREATE_FAILED": ("快照", "安全基线创建失败；未继续修改 Claude。"),
    "SNAPSHOT_ROOT_INIT_FAILED": ("快照", "无法安全初始化快照目录，未修改 Claude。"),
    "SNAPSHOT_ROOT_INSECURE": ("快照", "现有快照目录权限不可信，请复制详情后提交问题反馈。"),
    "SNAPSHOT_INVALID": ("快照", "安全快照不完整或已损坏，不能继续操作。"),
    "SNAPSHOT_MISMATCH": ("快照", "快照与当前 Claude 安装不匹配，不能用于恢复。"),
    "PERMISSION_DENIED": ("权限", "未能取得目标写权限，请查看权限步骤和命令退出码；关闭 Claude 仅会影响后续写入阶段。"),
    "ELEVATION_UNAVAILABLE": ("权限", "此副本不能安全提权，请安装正式安装包后重试。"),
    "ELEVATION_CANCELLED": ("权限", "管理员授权已取消，未修改 Claude。"),
    "ELEVATION_TAMPERED": ("权限", "提权环境未通过完整性检查，已停止操作。"),
    "PATCH_WRITE_FAILED": ("汉化", "写入中文资源失败；请复制详情后提交问题反馈。"),
    "PATCH_VERIFY_FAILED": ("汉化", "写入后的检测未通过，工具不会宣告汉化成功。"),
    "PATCH_ROLLBACK_FAILED": ("汉化", "自动回滚未完整完成，请不要再次修改并提交问题反馈。"),
    "PATCH_RECOVERY_FAILED": ("汉化", "未能安全清理上次未完成的汉化状态，请不要再次修改并提交问题反馈。"),
    "RESTORE_NO_MATCH": ("恢复", "没有与当前安装严格匹配的完整快照，未执行恢复。"),
    "RESTORE_PREFLIGHT_FAILED": ("恢复", "恢复前检查未通过，Claude 文件保持不变。"),
    "RESTORE_WRITE_FAILED": ("恢复", "恢复写入失败；请复制详情后提交问题反馈。"),
    "RESTORE_VERIFY_FAILED": ("恢复", "恢复后的完整性检测未通过。"),
    "RESTORE_ROLLBACK_FAILED": ("恢复", "恢复事务回退失败，请不要再次修改并提交问题反馈。"),
    "DETECT_FAILED": ("检测", "状态检测失败；请打开日志目录并提交问题反馈。"),
}

_SAFE_CONTEXT_KEYS = {
    "tool_version",
    "system_version",
    "claude_version",
    "install_type",
    "target_id",
    "profile",
    "resource",
    "replacement_count",
    "file_size",
    "hash_digest",
    "permission_step",
    "command_exit",
    "patch_step",
    "io_step",
    "rollback_content",
    "rollback_acl",
}
_VERSION_KEYS = {"tool_version", "claude_version"}
_INTEGER_KEYS = {
    "replacement_count",
    "file_size",
    "command_exit",
    "rollback_content",
    "rollback_acl",
}
_SIMPLE_VALUE = re.compile(r"^[\w .:+()\-/]{1,160}$", re.UNICODE)
_VERSION_VALUE = re.compile(r"^[A-Za-z0-9._+\-]{1,64}$")
_HASH_VALUE = re.compile(r"^[A-Fa-f0-9]{8,128}$")
_WIN_ERROR = re.compile(r"(?:WinError|errno)\s*[:\[]?\s*(\d+)", re.IGNORECASE)


def default_log_dir() -> Path:
    """Return the internal log directory without exposing it in log records."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "CC_Chinese" / "logs"
    return Path.home() / ".cc_chinese" / "logs"


def _safe_value(key: str, value: Any) -> str | int | None:
    if value is None:
        return None
    if key in _INTEGER_KEYS:
        return value if isinstance(value, int) and value >= 0 else None
    text = str(value).strip()
    if key in _VERSION_KEYS:
        return text if _VERSION_VALUE.fullmatch(text) else None
    if key == "hash_digest":
        return text.lower() if _HASH_VALUE.fullmatch(text) else None
    if key == "resource":
        candidate = Path(text.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
    if not _SIMPLE_VALUE.fullmatch(text):
        return None
    lowered = text.lower()
    if "bearer " in lowered or "token=" in lowered or "cookie=" in lowered:
        return None
    # A full user profile path is never useful diagnostic metadata.
    if re.search(r"[A-Za-z]:[/\\]Users[/\\][^/\\]+", text, re.IGNORECASE):
        return None
    return text


def sanitize_context(context: Mapping[str, Any] | None) -> dict[str, str | int]:
    """Copy only explicitly permitted, structurally safe metadata."""
    clean: dict[str, str | int] = {}
    for key, value in (context or {}).items():
        if key not in _SAFE_CONTEXT_KEYS:
            continue
        safe = _safe_value(key, value)
        if safe is not None:
            clean[key] = safe
    return clean


def _safe_cause(result: Mapping[str, Any]) -> str | None:
    """Classify a cause without persisting arbitrary exception text."""
    cause = result.get("exception")
    if isinstance(cause, BaseException):
        label = type(cause).__name__
        winerror = getattr(cause, "winerror", None)
        errno = getattr(cause, "errno", None)
        number = winerror if isinstance(winerror, int) else errno
        return f"{label}[{number}]" if isinstance(number, int) else label

    raw = result.get("cause") or result.get("log") or result.get("message")
    if not isinstance(raw, str):
        return None
    match = _WIN_ERROR.search(raw)
    if match:
        return f"OSError[{match.group(1)}]"
    for label in ("PermissionError", "FileNotFoundError", "TimeoutError", "OSError"):
        if label.lower() in raw.lower():
            return label
    return None


def normalize_error_code(action: str, result: Mapping[str, Any]) -> str:
    """Map legacy and new core results onto the finite public error vocabulary."""
    explicit = result.get("error_code") or result.get("code")
    if isinstance(explicit, str) and explicit in ERRORS:
        return explicit
    if result.get("state") == "missing":
        return "LOCATE_NOT_FOUND"
    if action == "restore":
        return "RESTORE_WRITE_FAILED"
    if action in {"install", "patch"}:
        return "PATCH_WRITE_FAILED"
    return "DETECT_FAILED"


@dataclass(frozen=True)
class DiagnosticReport:
    code: str
    stage: str
    advice: str
    action: str
    log_path: Path
    context: Mapping[str, str | int]

    def copy_text(self) -> str:
        """Return Issue-ready text containing no absolute log or target path."""
        lines = [
            "CC_Chinese - 故障详情",
            f"错误阶段：{self.stage}",
            f"错误码：{self.code}",
            f"建议：{self.advice}",
        ]
        labels = {
            "tool_version": "工具版本",
            "system_version": "系统版本",
            "claude_version": "Claude 版本",
            "install_type": "安装类型",
            "profile": "兼容配置",
            "target_id": "目标标识",
            "permission_step": "权限步骤",
            "command_exit": "命令退出码",
            "patch_step": "失败步骤",
            "io_step": "文件操作",
            "resource": "资源文件",
            "rollback_content": "内容回滚",
            "rollback_acl": "权限回滚",
        }
        for key, label in labels.items():
            if key in self.context:
                lines.append(f"{label}：{self.context[key]}")
        lines.append(f"日志文件：{self.log_path.name}")
        return "\n".join(lines)


class DiagnosticLogger:
    """Append-only JSON-lines logger with bounded local rotation."""

    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.directory = Path(directory) if directory is not None else default_log_dir()
        self.max_bytes = max(256, int(max_bytes))
        self.max_files = max(1, int(max_files))
        self.active_path = self.directory / "diagnostic.log"

    def _rotate(self, incoming_size: int) -> None:
        if not self.active_path.exists():
            return
        if self.active_path.stat().st_size + incoming_size <= self.max_bytes:
            return
        oldest = self.directory / f"diagnostic.log.{self.max_files - 1}"
        if oldest.exists():
            oldest.unlink()
        for index in range(self.max_files - 2, 0, -1):
            source = self.directory / f"diagnostic.log.{index}"
            if source.exists():
                source.replace(self.directory / f"diagnostic.log.{index + 1}")
        if self.max_files > 1:
            self.active_path.replace(self.directory / "diagnostic.log.1")
        else:
            self.active_path.unlink()

    def _encode(self, entry: Mapping[str, Any]) -> bytes:
        payload = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) <= self.max_bytes:
            return payload
        minimal = {
            "timestamp": entry["timestamp"],
            "action": entry["action"],
            "stage": entry["stage"],
            "error_code": entry["error_code"],
            "metadata_omitted": True,
        }
        return (json.dumps(minimal, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def record_failure(
        self,
        *,
        action: str,
        result: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> DiagnosticReport:
        code = normalize_error_code(action, result)
        stage, advice = ERRORS[code]
        safe_context = sanitize_context(context)
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action if action in {"install", "restore", "detect", "open", "elevation"} else "unknown",
            "stage": stage,
            "error_code": code,
            "context": safe_context,
        }
        cause = _safe_cause(result)
        if cause:
            entry["cause"] = cause
        payload = self._encode(entry)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._rotate(len(payload))
        with self.active_path.open("ab") as stream:
            stream.write(payload)
        return DiagnosticReport(code, stage, advice, action, self.active_path, safe_context)


def system_version() -> str:
    """Return a short OS label suitable for the allow-listed diagnostics context."""
    value = platform.platform(aliased=True, terse=True)
    return value[:160] if value else platform.system()
