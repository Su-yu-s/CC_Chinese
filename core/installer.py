"""Orchestrator: the single entry the GUI calls to install / restore / open.

Wires detector (locate + status) with the patch_* modules (the actual work) and
keeps a coherent progress stream for the UI. Imported as top-level modules via
sys.path (see main.py).
"""
from __future__ import annotations

from pathlib import Path

import detector
import patch_chunks
import patch_json
import restore


def _report(progress_cb, percent: int, message: str) -> None:
    if progress_cb:
        try:
            progress_cb(percent, message)
        except Exception:
            pass


def run_install(app_dir, progress_cb=None, backup=True) -> dict:
    """Close Claude → write JSON resources → write chunks → validate."""
    if not app_dir:
        return {
            "success": False,
            "state": "missing",
            "message": "未找到 Claude 安装目录。",
            "log": "未找到 Claude 安装目录。",
        }
    _report(progress_cb, 6, "准备：正在定位 Claude Desktop...")
    _report(progress_cb, 12, "正在关闭 Claude Desktop...")
    detector.stop_claude()

    _report(progress_cb, 20, "写入 zh-CN JSON 资源...")
    r1 = patch_json.run_patch(
        Path(app_dir),
        progress_cb=lambda p, m: _report(progress_cb, int(20 + p * 0.40), m),
        backup=backup,
    )
    if not r1.get("success"):
        return {"success": False, "state": "error", "message": r1.get("error", "JSON 资源补丁失败。"), "log": str(r1.get("error", ""))}

    _report(progress_cb, 64, "写入 chunk 文案与运行时增强...")
    r2 = patch_chunks.run_patch_chunks(
        Path(app_dir),
        progress_cb=lambda p, m: _report(progress_cb, int(64 + p * 0.36), m),
    )
    if not r2.get("success"):
        return {"success": False, "state": "error", "message": r2.get("error", "chunk 补丁失败。"), "log": str(r2.get("error", ""))}

    _report(progress_cb, 100, "中文补丁已安装。")
    return {
        "success": True,
        "state": "ready",
        "message": "汉化完成",
        "log": f"JSON 资源：{r1.get('copied', 0)} 个\nchunk 补丁：{r2.get('chunk_patches', 0)} 处\n耗时：{r1.get('duration_ms', 0)} ms",
    }


def run_restore(app_dir, progress_cb=None) -> dict:
    """Close Claude → restore official files → remove zh-CN artifacts + locale."""
    if not app_dir:
        return {
            "success": False,
            "state": "missing",
            "message": "未找到 Claude 安装目录。",
            "log": "未找到 Claude 安装目录。",
        }
    _report(progress_cb, 10, "正在关闭 Claude Desktop...")
    detector.stop_claude()

    r = restore.run_restore(
        Path(app_dir),
        progress_cb=lambda p, m: _report(progress_cb, int(20 + p * 0.80), m),
    )
    if not r.get("success"):
        return {"success": False, "state": "error", "message": r.get("error", "恢复失败。"), "log": str(r.get("error", ""))}

    _report(progress_cb, 100, "已恢复原样。")
    return {
        "success": True,
        "state": "repair",
        "message": "恢复完成",
        "log": f"恢复文件：{r.get('restored_files', 0)} 个\n清理残留：{r.get('artifacts_removed', 0)} 处",
    }


def run_open(app_dir, progress_cb=None) -> dict:
    """Launch Claude Desktop at the given install directory."""
    if progress_cb:
        _report(progress_cb, 40, "正在打开 Claude Desktop...")
    ok, detail = detector.open_claude(app_dir)
    _report(progress_cb, 100, detail)
    return {
        "success": ok,
        "state": "ready" if ok else "error",
        "message": "Claude Desktop 已启动。" if ok else detail,
        "log": detail,
    }


def check_update(app_dir, progress_cb=None) -> dict:
    """Read current Claude version + localization status (a query, not a patch)."""
    status = detector.build_status(app_dir=app_dir)
    _report(progress_cb, 100, f"当前版本 {status['version']} 已可用。")
    return {
        "success": True,
        "state": status["state"],
        "message": f"当前版本 {status['version']} 已可用。",
        "log": f"当前版本：{status['version']}\n安装路径：{status['installPath']}\nClaude 官方更新后请重新安装补丁。",
    }
