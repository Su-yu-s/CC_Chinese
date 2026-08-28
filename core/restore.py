#!/usr/bin/env python3
"""Restore Claude Desktop files from backup, remove zh-CN artifacts, and remove locale setting.

Accepts --app-dir to specify the Claude app directory dynamically.
If not provided, auto-detects WindowsApps and AppData\\Local\\AnthropicClaude installs.

Restores backed-up files (relative to app\\resources) and removes
locale=zh-CN from user config.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import patch_chunks as patch_chunks_zh_cn
from best_effort_io import ensure_admin_for_windowsapps, print_permission_denied_hint
from detector import resolve_claude_config


BACKUP_BASE = Path(os.environ["LOCALAPPDATA"]) / "Claude-zh-CN-official-backup"
BACKUP_JSON_ONLY = BACKUP_BASE / "json-only"
CONFIG_PATH = resolve_claude_config()
FONT_KEY = "claudeZhCnFont"
SKIP_RESTORE_NAMES = {"app.asar", patch_chunks_zh_cn.CHUNK_STATE_NAME}


def find_claude_package() -> Path | None:
    """Auto-detect Claude app directory from supported Windows install layouts."""
    appx = find_appx_claude_package()
    if appx:
        return appx

    windows_candidates: list[Path] = []
    windowsapps = Path(r"C:\Program Files\WindowsApps")
    if windowsapps.exists():
        windows_candidates.extend(
            path.parent.parent
            for path in windowsapps.glob("Claude_*_x64__*/app/resources/en-US.json")
            if path.is_file()
        )
    if windows_candidates:
        return sorted(set(windows_candidates), key=lambda path: (windowsapps_version_key(path), str(path)), reverse=True)[0]

    local_candidates: list[Path] = []
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        anthropic = Path(localappdata) / "AnthropicClaude"
        if anthropic.exists():
            local_resource_files = [
                anthropic / "resources" / "en-US.json",
                anthropic / "app" / "resources" / "en-US.json",
                *anthropic.glob("app*/resources/en-US.json"),
            ]
            local_candidates.extend(path.parent.parent for path in local_resource_files if path.is_file())

    if not local_candidates:
        return None
    return sorted(set(local_candidates), key=lambda path: (path.stat().st_mtime if path.exists() else 0, str(path)), reverse=True)[0]


def find_appx_claude_package() -> Path | None:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$p=Get-AppxPackage -Name Claude -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1; if ($p) { Join-Path $p.InstallLocation 'app' }",
            ],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    for line in result.stdout.splitlines():
        app_dir = Path(line.strip())
        if (app_dir / "resources" / "en-US.json").is_file():
            return app_dir
    return None


def windowsapps_version_key(app_dir: Path) -> tuple[int, ...]:
    parts = app_dir.parent.name.split("_")
    if len(parts) < 2:
        return ()
    version: list[int] = []
    for part in parts[1].split("."):
        try:
            version.append(int(part))
        except ValueError:
            version.append(0)
    return tuple(version)


def iter_assets_dirs(app_resources: Path) -> list[Path]:
    """Return all discovered ion-dist/assets version directories."""
    assets_root = app_resources / "ion-dist" / "assets"
    if not assets_root.exists():
        return []

    dirs = {
        path.parent
        for path in assets_root.rglob("index-*.js")
        if path.is_file()
    }
    return sorted(dirs, key=lambda path: str(path).lower(), reverse=True)


def restore_from(backup_root: Path, app_resources: Path) -> int:
    """Restore files from backup to app/resources."""
    restored = 0
    for src in backup_root.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(backup_root)
        if rel.name in SKIP_RESTORE_NAMES:
            continue
        dst = app_resources / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if copy2_best_effort(src, dst, context="restore backup"):
            restored += 1
    return restored


def remove_chunk_state() -> bool:
    """Invalidate the fast-path cache whenever official bundles are restored."""
    path = patch_chunks_zh_cn.chunk_state_path()
    try:
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed
    except OSError as exc:
        print(f"Warning: cannot remove chunk cache at {path}: {exc}")
        return False


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
            print_permission_denied_hint(dst)
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
            print_permission_denied_hint(path)
            return False
    except OSError as e:
        print(f"Warning: cannot write {context} at {path}: {e}; skipping")
        return False


def remove_zh_cn_artifacts(app_resources: Path) -> tuple[int, int]:
    """Remove zh-CN resources and scrub whitelist entries from bundles."""
    deleted = 0
    scrubbed = 0

    targets = [
        app_resources / "zh-CN.json",
        app_resources / "ion-dist" / "i18n" / "zh-CN.json",
        app_resources / "ion-dist" / "i18n" / "statsig" / "zh-CN.json",
    ]
    for path in targets:
        if not path.exists():
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as e:
            print(f"Warning: cannot delete {path}: {e}; skipping")

    for assets_dir in iter_assets_dirs(app_resources):
        for path in sorted(assets_dir.glob("index-*.js")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as e:
                print(f"Warning: cannot read {path}: {e}; skipping")
                continue

            if ',"zh-CN"' not in content:
                continue

            if write_text_best_effort(
                path,
                content.replace(',"zh-CN"', ''),
                context="remove zh-CN whitelist",
            ):
                scrubbed += 1

    return deleted, scrubbed


def revert_chunk_translations(app_resources: Path) -> int:
    """反向退掉所有 chunk 硬编码中文(中文->英文)。

    合并全部 PATCHES 的反向对,对每个 js 一次性扫描替换。相比按 pattern 分组,
    即使 chunk 文件名 hash 随版本变化、或某 pattern 漏配,也能退干净所有中文文案。
    """
    assets_dirs = iter_assets_dirs(app_resources)
    if not assets_dirs:
        return 0

    reverse_pairs: dict[str, str] = {}
    for pattern, replacements in patch_chunks_zh_cn.PATCHES.items():
        for old, new in replacements:
            if old != new:
                # A canonical English source is listed before legacy Chinese
                # aliases that normalize to the same translation.  Keep that
                # first source so restore does not turn the current wording
                # back into an obsolete translation.
                reverse_pairs.setdefault(new, old)
    if not reverse_pairs:
        return 0
    pairs = list(reverse_pairs.items())

    changed_files = 0
    for assets_dir in assets_dirs:
        for path in sorted(assets_dir.glob("*.js")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            content, replacement_count = patch_chunks_zh_cn.replace_outside_injected_blocks(
                content,
                pairs,
            )
            if replacement_count and write_text_best_effort(path, content, context="revert chunk translations"):
                changed_files += 1

    return changed_files


def cleanup_known_chunk_residue_tokens(app_resources: Path) -> int:
    """Fallback cleanup for known visible labels that may survive stale backups."""
    assets_dirs = iter_assets_dirs(app_resources)
    if not assets_dirs:
        return 0

    cleanup_pairs = [
        ('children:"\u9879\u76ee"', 'children:"Project"'),
        ('label:"\u9879\u76ee"', 'label:"Projects"'),
        ('["project","\u9879\u76ee"]', '["project","Project"]'),
        ('label:"\u5df2\u5b89\u6392"', 'label:"Scheduled"'),
        ('const Ea="\u5df2\u5b89\u6392"', 'const Ea="Scheduled"'),
        ('title:"\u8ba1\u5212\u4efb\u52a1",subheader', 'title:"Scheduled tasks",subheader'),
        ('message:"\u8ba1\u5212\u4efb\u52a1\u4ec5\u5728\u8ba1\u7b97\u673a\u4fdd\u6301\u5524\u9192\u65f6\u8fd0\u884c\u3002"', 'message:"Scheduled tasks only run while your computer is awake."'),
        ('children:"\u65b0\u5efa\u4efb\u52a1"', 'children:"New task"'),
        ('?"\u65b0\u5efa\u4efb\u52a1":"\u65b0\u5efa\u804a\u5929"', '?"New task":"New chat"'),
        ('baseDescription:"\u65b0\u5efa\u4efb\u52a1"', 'baseDescription:"New task"'),
        ('label:"\u529e\u516c"', 'label:"Cowork"'),
        ('label:"\u534f\u4f5c"', 'label:"Cowork"'),
        ('label:"\u7f16\u7801"', 'label:"Code"'),
        ('label:"\u4ee3\u7801"', 'label:"Code"'),
        ('label:"\u81ea\u5b9a\u4e49"', 'label:"Customize"'),
        ('label:"\u5b9e\u65f6\u4f5c\u54c1"', 'label:"Live artifacts"'),
        ('label:"\u5b9e\u65f6\u5de5\u4ef6"', 'label:"Live artifacts"'),
        ('label:"\u5b9e\u65f6 Artifacts"', 'label:"Live artifacts"'),
        ('"\u4f5c\u54c1"', '"Artifacts"'),
        ('"\u5de5\u4ef6"', '"Artifacts"'),
        ('children:"\u5df2\u56fa\u5b9a"', 'children:"Pinned"'),
        ('children:"\u62d6\u62fd\u56fa\u5b9a"', 'children:"Drag to pin"'),
        ('const Co="\u6700\u8fd1"', 'const Co="Recents"'),
        ('title:"\u4ee3\u7801\u6267\u884c\u4e0e\u6587\u4ef6\u521b\u5efa"', 'title:"Code execution and file creation"'),
    ]

    changed_files = 0
    for assets_dir in assets_dirs:
        for path in sorted(assets_dir.glob("index-*.js")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as e:
                print(f"Warning: cannot read {path}: {e}; skipping")
                continue

            changed = False
            for old, new in cleanup_pairs:
                if old in content:
                    content = content.replace(old, new)
                    changed = True

            if changed and write_text_best_effort(path, content, context="cleanup chunk residues"):
                changed_files += 1

    return changed_files


def remove_locale() -> bool:
    """Remove locale=zh-CN and zh-CN font mirror from user config."""
    if not CONFIG_PATH.exists():
        return False

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    changed = False
    if "locale" in data:
        del data["locale"]
        changed = True
    if FONT_KEY in data:
        del data[FONT_KEY]
        changed = True

    if not changed:
        return False

    return write_text_best_effort(
        CONFIG_PATH,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        context="restore locale config",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Claude Desktop from backup")
    parser.add_argument("--app-dir", type=str, default=None,
                        help="Path to Claude app directory (auto-detected if omitted)")
    args = parser.parse_args()

    if args.app_dir:
        app_dir = Path(args.app_dir)
    else:
        app_dir = find_claude_package()

    if not app_dir or not app_dir.exists():
        raise SystemExit("Claude app directory not found. Use --app-dir to specify manually.")

    elevation_exit = ensure_admin_for_windowsapps(
        app_dir,
        Path(__file__).resolve(),
        sys.argv[1:],
    )
    if elevation_exit is not None:
        return elevation_exit

    app_resources = app_dir / "resources"

    # Also check for full-patch backups (legacy)
    backup_full = None
    for d in sorted(BACKUP_BASE.glob("Claude_*"), reverse=True):
        if d.is_dir() and any(d.rglob("*")):
            backup_full = d
            break

    # Check for chunk backups
    backup_chunks = BACKUP_BASE / "chunks"

    candidates = []
    if BACKUP_JSON_ONLY.exists() and any(BACKUP_JSON_ONLY.rglob("*")):
        candidates.append(("json-only", BACKUP_JSON_ONLY, app_resources))
    if backup_chunks.exists() and any(backup_chunks.rglob("*")):
        assets_dir = app_resources / "ion-dist" / "assets" / "v1"
        candidates.append(("chunks", backup_chunks, assets_dir))
    if backup_full and not candidates:
        candidates.append(("full-patch", backup_full, app_resources))

    if not candidates:
        raise SystemExit(f"No backup found under {BACKUP_BASE}")

    total_restored = 0
    for label, root, target in candidates:
        count = restore_from(root, target)
        total_restored += count
        print(f"  Restored from {label}: {root} ({count} files)")

    deleted, scrubbed = remove_zh_cn_artifacts(app_resources)
    reverted = revert_chunk_translations(app_resources)
    cleaned = cleanup_known_chunk_residue_tokens(app_resources)

    # Remove locale
    locale_removed = remove_locale()
    chunk_state_removed = remove_chunk_state()

    print()
    print("Done")
    print(f"Total restored files: {total_restored}")
    print(f"Zh-CN artifacts removed: {deleted}")
    print(f"Whitelist bundles scrubbed: {scrubbed}")
    print(f"Chunk files reverted: {reverted}")
    print(f"Chunk residue cleanup: {cleaned}")
    print(f"Locale removed: {locale_removed}")
    print(f"Chunk cache removed: {chunk_state_removed}")
    return 0


def run_restore(app_dir, progress_cb=None) -> dict:
    """Programmatic entry: restore official files + remove zh-CN artifacts + locale.

    Unlike main(), returns a dict and does NOT self-elevate. progress_cb is an
    optional callable(percent, message).
    """
    import time

    def report(percent: int, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(percent, message)
            except Exception:
                pass

    t0 = time.time()
    app_dir = Path(app_dir)
    app_resources = app_dir / "resources"
    if not app_resources.exists():
        return {
            "success": False,
            "error": f"App resources not found: {app_resources}",
            "duration_ms": int((time.time() - t0) * 1000),
        }

    report(10, "从官方备份恢复资源...")
    backup_full = None
    for d in sorted(BACKUP_BASE.glob("Claude_*"), reverse=True):
        if d.is_dir() and any(d.rglob("*")):
            backup_full = d
            break
    backup_chunks = BACKUP_BASE / "chunks"

    candidates = []
    if BACKUP_JSON_ONLY.exists() and any(BACKUP_JSON_ONLY.rglob("*")):
        candidates.append(("json-only", BACKUP_JSON_ONLY, app_resources))
    if backup_chunks.exists() and any(backup_chunks.rglob("*")):
        assets_dir = app_resources / "ion-dist" / "assets" / "v1"
        candidates.append(("chunks", backup_chunks, assets_dir))
    if backup_full and not candidates:
        candidates.append(("full-patch", backup_full, app_resources))

    if not candidates:
        return {
            "success": False,
            "error": f"No backup found under {BACKUP_BASE}",
            "restored_files": 0,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    total_restored = 0
    for label, root, target in candidates:
        count = restore_from(root, target)
        total_restored += count
        report(35, f"已从 {label} 恢复 {count} 个文件")

    report(55, "移除 zh-CN 残留与白名单补丁...")
    deleted, _ = remove_zh_cn_artifacts(app_resources)
    reverted = revert_chunk_translations(app_resources)
    cleaned = cleanup_known_chunk_residue_tokens(app_resources)
    report(80, "移除 locale 与字体镜像...")
    locale_removed = remove_locale()
    chunk_state_removed = remove_chunk_state()
    report(100, "恢复完成")

    return {
        "success": True,
        "error": None,
        "restored_files": total_restored,
        "artifacts_removed": deleted,
        "chunks_reverted": reverted,
        "chunk_residue_cleaned": cleaned,
        "locale_removed": locale_removed,
        "chunk_state_removed": chunk_state_removed,
        "duration_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    raise SystemExit(main())
