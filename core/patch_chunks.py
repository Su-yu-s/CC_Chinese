#!/usr/bin/env python3
"""Patch JS chunks with Chinese UI labels.

This script applies safe string replacements to hardcoded UI labels
in Claude Desktop's JS bundle files. It backs up original files before
modifying and only replaces exact string patterns.

Run after patch_windowsapps_json_only.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from best_effort_io import ensure_admin_for_windowsapps, print_permission_denied_hint
from safe_io import atomic_copy, atomic_write_text


BACKUP_ROOT = Path(os.environ["LOCALAPPDATA"]) / "Claude-zh-CN-official-backup" / "chunks"
STATE_ROOT = Path(os.environ["LOCALAPPDATA"]) / "CC_Chinese" / "state"
CHUNK_STATE_NAME = "chunk-state.json"
CHUNK_STATE_SCHEMA = 1

# 补丁指纹标记：官方 bundle 绝不包含，是“已打补丁”的可靠指纹。
# 早期版本注入的功能运行时（字体管理、会话快捷操作、对话导出、移动到项目、
# 阅读布局）已整体移除，现在只向入口 bundle 追加这一个纯注释指纹。
PATCH_MARKER_BEGIN = "// __CLAUDE_ZH_CN_PATCH_BEGIN__"
PATCH_MARKER_END = "// __CLAUDE_ZH_CN_PATCH_END__"
PATCH_MARKER_SCRIPT = (
    f"{PATCH_MARKER_BEGIN}\n"
    "// Claude zh-CN patch fingerprint. The official bundle never contains it.\n"
    f"{PATCH_MARKER_END}\n"
)
# 旧版功能运行时的注入块标记：重打补丁时用于把它们从入口 bundle 中剥离干净。
LEGACY_BLOCK_MARKERS = (
    ("// __CLAUDE_ZH_CN_FONT_PATCH_BEGIN__", "// __CLAUDE_ZH_CN_FONT_PATCH_END__"),
    ("// __CLAUDE_ZH_CN_SESSION_DELETE_PATCH_BEGIN__", "// __CLAUDE_ZH_CN_SESSION_DELETE_PATCH_END__"),
)
LEGACY_RUNTIME_FLAGS = (
    "__CLAUDE_ZH_CN_FONT_PATCH__",
    "__CLAUDE_ZH_CN_SESSION_DELETE_PATCH__",
)
INJECTED_BLOCK_MARKERS = ((PATCH_MARKER_BEGIN, PATCH_MARKER_END),) + LEGACY_BLOCK_MARKERS


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


def find_claude_package() -> Path | None:
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


def find_assets_dir(app_resources: Path) -> Path | None:
    """Locate the active ion-dist/assets version directory."""
    assets_root = app_resources / "ion-dist" / "assets"
    if not assets_root.exists():
        return None

    candidates = sorted(
        {path.parent for path in assets_root.rglob("index-*.js") if path.is_file()},
        key=lambda path: str(path).lower(),
        reverse=True,
    )
    return candidates[0] if candidates else None


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


def chunk_state_path() -> Path:
    """Return the tool cache path; it is never part of a restore backup."""
    return STATE_ROOT / CHUNK_STATE_NAME


def normalize_app_dir(app_dir: Path) -> str:
    return os.path.normcase(str(Path(app_dir).resolve()))


def patch_signature() -> str:
    """Hash every input that can change the resulting JS patch."""
    payload = {
        "patches": PATCHES,
        "marker_script": PATCH_MARKER_SCRIPT,
        "markers": INJECTED_BLOCK_MARKERS,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assets_fingerprint(app_resources: Path) -> str:
    """Fingerprint JS metadata without reading hundreds of megabytes of bundles."""
    assets_root = app_resources / "ion-dist" / "assets"
    digest = hashlib.sha256()
    for assets_dir in iter_assets_dirs(app_resources):
        for path in sorted(assets_dir.glob("*.js"), key=lambda item: item.name.lower()):
            try:
                stat_result = path.stat()
            except OSError:
                continue
            rel = path.relative_to(assets_root).as_posix()
            digest.update(rel.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(stat_result.st_mtime_ns).encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def load_chunk_state() -> dict | None:
    path = chunk_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict) or state.get("schema") != CHUNK_STATE_SCHEMA:
        return None
    return state


def save_chunk_state(state: dict) -> bool:
    """Atomically persist the cache so interruption cannot leave partial JSON."""
    path = chunk_state_path()
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        print(f"Warning: cannot save chunk cache at {path}: {exc}")
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def has_complete_runtime_markers(app_resources: Path) -> bool:
    """Detect installations already carrying this tool's runtime fingerprint.

    Accepts either the current fingerprint marker or the pair of legacy
    feature-runtime markers, so installs patched by older releases are still
    recognized (and upgraded instead of re-scanned from scratch).
    """
    index_files = [
        path
        for assets_dir in iter_assets_dirs(app_resources)
        for path in sorted(assets_dir.glob("index-*.js"))
    ]
    if not index_files:
        return False
    legacy_required = tuple(begin for begin, _ in LEGACY_BLOCK_MARKERS)
    for path in index_files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        if PATCH_MARKER_BEGIN in content:
            continue
        if all(begin in content for begin in legacy_required):
            continue
        return False
    return True


def stable_prefix_pattern(pattern: str) -> str | None:
    """Convert a stale hashed filename into a narrow, version-stable glob."""
    if any(char in pattern for char in "*?[") or pattern.startswith("__"):
        return None
    match = re.fullmatch(r"([A-Za-z0-9]+)-[^/\\]+\.js", Path(pattern).name)
    return f"{match.group(1)}-*.js" if match else None


def content_outside_injected_blocks(content: str) -> str:
    """Remove injected blocks before target discovery to avoid matching ourselves."""
    pieces: list[str] = []
    cursor = 0
    while cursor < len(content):
        candidates = []
        for begin, end in INJECTED_BLOCK_MARKERS:
            start = content.find(begin, cursor)
            if start >= 0:
                candidates.append((start, begin, end))
        if not candidates:
            pieces.append(content[cursor:])
            break
        start, begin, end = min(candidates, key=lambda item: item[0])
        pieces.append(content[cursor:start])
        block_end = content.find(end, start + len(begin))
        if block_end < 0:
            break
        cursor = block_end + len(end)
    return "".join(pieces)


def backup_file(path: Path, assets_dir: Path) -> bool:
    """Back up relative to app/resources so assets/v1 and v2 never collide."""
    if not path.exists():
        return True
    try:
        app_resources = assets_dir.parents[2]
        rel = path.relative_to(app_resources)
    except (IndexError, ValueError):
        return False
    dst = BACKUP_ROOT / rel
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if dst.exists():
        return True
    return copy2_best_effort(path, dst, context="backup file")


def copy2_best_effort(src: Path, dst: Path, *, context: str) -> bool:
    """Copy a file and retry once after clearing the destination readonly bit."""
    try:
        atomic_copy(src, dst)
        return True
    except PermissionError:
        if dst.exists():
            try:
                dst.chmod(dst.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
        try:
            atomic_copy(src, dst)
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
        atomic_write_text(path, text)
        return True
    except PermissionError:
        try:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass
        try:
            atomic_write_text(path, text)
            return True
        except OSError as e:
            print(f"Warning: cannot write {context} at {path}: {e}; skipping")
            print_permission_denied_hint(path)
            return False
    except OSError as e:
        print(f"Warning: cannot write {context} at {path}: {e}; skipping")
        return False


def _strip_legacy_runtime_blocks(content: str, path: Path) -> tuple[str, int]:
    """Remove feature runtimes injected by older releases from an entry bundle.

    Covers both the ``BEGIN``/``END`` comment blocks and the oldest
    ``;(()=>{ ... })();`` marker form.  An unterminated block aborts the patch
    instead of truncating the bundle, matching the previous safety behavior.
    """
    removed = 0
    for begin, end in LEGACY_BLOCK_MARKERS:
        while begin in content:
            start = content.index(begin)
            end_pos = content.find(end, start)
            if end_pos < 0:
                raise RuntimeError(
                    f"Incomplete legacy runtime marker in {path}; refusing to truncate the bundle"
                )
            content = content[:start].rstrip() + "\n" + content[end_pos + len(end):].lstrip()
            removed += 1
    for flag in LEGACY_RUNTIME_FLAGS:
        while flag in content:
            marker_pos = content.index(flag)
            start = content.rfind(";(()=>{", 0, marker_pos)
            if start == -1:
                start = marker_pos
            legacy_end = content.find("})();", marker_pos)
            if legacy_end == -1:
                raise RuntimeError(
                    f"Incomplete legacy runtime marker in {path}; refusing to truncate the bundle"
                )
            content = content[:start].rstrip() + "\n" + content[legacy_end + len("})();"):].lstrip()
            removed += 1
    return content, removed


def patch_entry_runtime(assets_dir: Path, *, backup: bool = True) -> int:
    """Strip legacy feature runtimes and stamp the patch fingerprint marker."""
    candidates = sorted(assets_dir.glob("index-*.js"))
    if not candidates:
        print("Warning: no index-*.js found; skipping entry runtime patch")
        return 0

    changed = 0
    for path in candidates:
        if backup and not backup_file(path, assets_dir):
            raise OSError(f"Failed to back up entry runtime target: {path}")
        content = path.read_text(encoding="utf-8")
        new_content, removed = _strip_legacy_runtime_blocks(content, path)
        if PATCH_MARKER_BEGIN not in new_content:
            new_content = new_content.rstrip() + "\n" + PATCH_MARKER_SCRIPT
        if new_content == content:
            continue
        if write_text_best_effort(path, new_content, context="entry runtime patch"):
            changed += 1
            print(f"  {path.name}: entry runtime updated (legacy blocks removed: {removed})")
    return changed


def replace_outside_injected_blocks(content: str, replacements: list[tuple[str, str]]) -> tuple[str, int]:
    """Apply bundle replacements without rewriting this patcher's injected JS."""

    def replace_segment(segment: str) -> tuple[str, int]:
        changed = 0
        for old, new in replacements:
            if old in segment and old != new:
                segment = segment.replace(old, new)
                changed += 1
        return segment, changed

    pieces: list[str] = []
    changed = 0
    cursor = 0
    while cursor < len(content):
        candidates = []
        for begin, end in INJECTED_BLOCK_MARKERS:
            start = content.find(begin, cursor)
            if start >= 0:
                candidates.append((start, begin, end))
        if not candidates:
            tail, count = replace_segment(content[cursor:])
            pieces.append(tail)
            changed += count
            break

        start, begin, end = min(candidates, key=lambda item: item[0])
        prefix, count = replace_segment(content[cursor:start])
        pieces.append(prefix)
        changed += count

        block_end = content.find(end, start + len(begin))
        if block_end < 0:
            pieces.append(content[start:])
            cursor = len(content)
            break
        block_end += len(end)
        pieces.append(content[start:block_end])
        cursor = block_end

    return "".join(pieces), changed


def _apply_replacements(
    fpath: Path,
    replacements: list[tuple[str, str]],
    assets_dir: Path,
    *,
    backup: bool = True,
) -> int:
    """对单个 chunk js 应用替换并写回,返回成功替换数。"""
    if backup and not backup_file(fpath, assets_dir):
        raise OSError(f"Failed to back up chunk target: {fpath}")
    content = fpath.read_text(encoding="utf-8")
    content, changed = replace_outside_injected_blocks(content, replacements)
    if changed > 0 and write_text_best_effort(fpath, content, context="chunk replacement"):
        print(f"  {fpath.name}: {changed} replacements")
        return changed
    return 0


def patch_assets_tree(
    app_resources: Path,
    progress_cb=None,
    *,
    mode: str = "full",
    target_sink: set[str] | None = None,
    backup: bool = True,
) -> int:
    """Patch every assets directory, using narrow prefix discovery when possible.

    ``mode="upgrade"`` is for installations carrying both runtime markers from
    an older release. It refreshes known targets and runtimes without a global
    content scan.
    """
    assets_dirs = iter_assets_dirs(app_resources)
    if not assets_dirs:
        print("Warning: assets root not found; skipping chunk patches")
        return 0

    def report(percent: int, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(percent, message)
            except Exception:
                pass

    def remember(path: Path, assets_dir: Path) -> None:
        if target_sink is not None:
            target_sink.add(f"{assets_dir.name}/{path.relative_to(assets_dir).as_posix()}")

    total = 0
    for assets_dir in assets_dirs:
        direct = []
        needle_groups = []
        for pattern, replacements in PATCHES.items():
            files = sorted(assets_dir.glob(pattern))
            if not files:
                prefix_pattern = stable_prefix_pattern(pattern)
                if prefix_pattern:
                    files = sorted(assets_dir.glob(prefix_pattern))
            if files:
                direct.append((pattern, replacements, files))
            else:
                needles = [old for old, new in replacements if old != new]
                if needles:
                    needle_groups.append((pattern, replacements, needles))

        for _, replacements, files in direct:
            for fpath in files:
                remember(fpath, assets_dir)
                total += _apply_replacements(fpath, replacements, assets_dir, backup=backup)

        if needle_groups and mode == "full":
            report(25, "首次适配当前版本，正在定位入口 JS chunk...")
            matched: dict[str, dict[Path, set[str]]] = {
                pattern: {} for pattern, _, _ in needle_groups
            }
            needle_owners: dict[str, set[str]] = {}
            for pattern, _, needles in needle_groups:
                for needle in needles:
                    needle_owners.setdefault(needle, set()).add(pattern)
            # A single alternation search keeps the cold path linear in bundle
            # size. The previous nested ``needle in content`` loop could scan
            # each 300+ MB asset tree hundreds of times.
            matcher = re.compile(
                "|".join(
                    re.escape(needle)
                    for needle in sorted(needle_owners, key=len, reverse=True)
                )
            )
            # Unknown hashed chunks are covered by the injected runtime text
            # repair. Limit the static fallback to entry bundles so a Claude
            # update never causes a 300+ MB full-tree scan.
            all_js = sorted(assets_dir.glob("index-*.js"))
            for index, path in enumerate(all_js, start=1):
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                searchable = content_outside_injected_blocks(content)
                found_needles: dict[str, set[str]] = {}
                for match in matcher.finditer(searchable):
                    needle = match.group(0)
                    for pattern in needle_owners[needle]:
                        found_needles.setdefault(pattern, set()).add(needle)
                for pattern, needles in found_needles.items():
                    matched[pattern][path] = needles
                if index % 200 == 0 or index == len(all_js):
                    percent = 25 + int(45 * index / max(len(all_js), 1))
                    report(percent, f"首次定位入口 chunk：{index}/{len(all_js)}")
            by_pattern = {pattern: replacements for pattern, replacements, _ in needle_groups}
            for pattern, files in matched.items():
                for fpath, found_needles in files.items():
                    remember(fpath, assets_dir)
                    replacements = [
                        pair for pair in by_pattern[pattern] if pair[0] in found_needles
                    ]
                    total += _apply_replacements(fpath, replacements, assets_dir, backup=backup)
        elif needle_groups:
            report(60, "已识别旧版补丁，跳过全量 JS 扫描")

        for path in sorted(assets_dir.glob("index-*.js")):
            remember(path, assets_dir)
        total += patch_entry_runtime(assets_dir, backup=backup)

    return total


PATCHES: dict[str, list[tuple[str, str]]] = {}

# Current desktop navigation/provider labels live in shared chunks. Match
# their semantic keys rather than replacing product names or icon names.
PATCHES["shared-*.js"] = [
    ('key:"task",label:"Cowork"', 'key:"task",label:"办公"'),
    ('key:"code",label:"Code"', 'key:"code",label:"编码"'),
    ('mode:"cowork",icon:"Tasks",label:"Cowork"', 'mode:"cowork",icon:"Tasks",label:"办公"'),
    ('mode:"code",icon:"Code",label:"Code"', 'mode:"code",icon:"Code",label:"编码"'),
    ('gateway:"Gateway",anthropic:', 'gateway:"第三方API",anthropic:'),
]

# === 3P settings page (c71860c77-DNv5VYLZ.js) ===
PATCHES["c71860c77-DNv5VYLZ.js"] = [
    ('"Egress Requirements"', '"\u51fa\u53e3\u8981\u6c42"'),
    ('"Gateway base URL"', '"\u7b2c\u4e09\u65b9 URL"'),
    ('"Gateway API key"', '"\u7b2c\u4e09\u65b9 API Key"'),
    ('"Gateway auth scheme"', '"\u7b2c\u4e09\u65b9\u8ba4\u8bc1\u65b9\u5f0f"'),
    ('"Gateway extra headers"', '"\u7b2c\u4e09\u65b9\u989d\u5916\u8bf7\u6c42\u5934"'),
    ('"Allow desktop extensions"', '"\u5141\u8bb8\u684c\u9762\u6269\u5c55"'),
    ('"Show extension directory"', '"\u663e\u793a\u6269\u5c55\u76ee\u5f55"'),
    ('"Require signed extensions"', '"\u8981\u6c42\u6269\u5c55\u7b7e\u540d"'),
    ('"Allow user-added MCP servers"', '"\u5141\u8bb8\u7528\u6237\u6dfb\u52a0 MCP \u670d\u52a1\u5668"'),
    ('"Allow Claude Code tab"', '"\u5141\u8bb8 Claude Code \u6807\u7b7e\u9875"'),
    ('"Secure VM features"', '"\u5b89\u5168\u865a\u62df\u673a\u529f\u80fd"'),
    ('"Require full VM sandbox"', '"\u8981\u6c42\u5b8c\u6574\u865a\u62df\u673a\u6c99\u76d2"'),
    ('"Allowed egress hosts"', '"\u5141\u8bb8\u7684\u51fa\u53e3\u4e3b\u673a"'),
    ('"OpenTelemetry collector endpoint"', '"OpenTelemetry \u91c7\u96c6\u5668\u7aef\u70b9"'),
    ('"OpenTelemetry exporter protocol"', '"OpenTelemetry \u5bfc\u51fa\u534f\u8bae"'),
    ('"OpenTelemetry exporter headers"', '"OpenTelemetry \u5bfc\u51fa\u8bf7\u6c42\u5934"'),
    ('"Auto-update enforcement window"', '"\u81ea\u52a8\u66f4\u65b0\u5f3a\u5236\u7a97\u53e3"'),
    ('"Block auto-updates"', '"\u7981\u6b62\u81ea\u52a8\u66f4\u65b0"'),
    ('"Skip login-mode chooser"', '"\u8df3\u8fc7\u767b\u5f55\u6a21\u5f0f\u9009\u62e9"'),
    ('"Required organization"', '"\u5fc5\u9700\u7684\u7ec4\u7ec7"'),
    ('"Inference provider"', '"\u63a8\u7406\u4f9b\u5e94\u5546"'),
    ('"Connection"', '"\u8fde\u63a5\u65b9\u5f0f"'),
    ('"Sandbox & workspace"', '"\u6c99\u76d2\u4e0e\u5de5\u4f5c\u533a"'),
    ('"Connectors & extensions"', '"\u8fde\u63a5\u5668\u4e0e\u6269\u5c55"'),
    ('"Telemetry & updates"', '"\u9065\u6d4b\u4e0e\u66f4\u65b0"'),
    ('"Usage limits"', '"\u4f7f\u7528\u9650\u5236"'),
    ('"Plugins & skills"', '"\u63d2\u4ef6\u4e0e\u6280\u80fd"'),
    ('gateway:"Gateway"', 'gateway:"\u7b2c\u4e09\u65b9API"'),
    ('gateway:"\u81ea\u5b9a\u4e49"', 'gateway:"\u7b2c\u4e09\u65b9API"'),
    ('gateway:"\u7b2c\u4e09\u65b9"', 'gateway:"\u7b2c\u4e09\u65b9API"'),
]

# === Hardcoded UI strings that moved out of i18n JSON in recent builds ===
# Use a deliberately non-matching file name so find_patch_targets scans JS chunks
# and only touches files that actually contain one of these exact needles.
PATCHES["__claude_zh_cn_hardcoded_ui__.js"] = [
    ('defaultMessage:"New",id:"bW7B87wFFp"', 'defaultMessage:"\u65b0\u5efa",id:"bW7B87wFFp"'),
    ('defaultMessage:"\u65b0",id:"bW7B87wFFp"', 'defaultMessage:"\u65b0\u5efa",id:"bW7B87wFFp"'),
    ('defaultMessage:"Projects",id:"UxTJRaKagI"', 'defaultMessage:"\u9879\u76ee",id:"UxTJRaKagI"'),
    ('defaultMessage:"Artifacts",id:"eW5eoWkxy3"', 'defaultMessage:"\u4f5c\u54c1",id:"eW5eoWkxy3"'),
    ('defaultMessage:"Scheduled",id:"cXAlMRerxW"', 'defaultMessage:"\u5b9a\u65f6",id:"cXAlMRerxW"'),
    ('defaultMessage:"\u5df2\u5b89\u6392",id:"cXAlMRerxW"', 'defaultMessage:"\u5b9a\u65f6",id:"cXAlMRerxW"'),
    ('defaultMessage:"Customize",id:"TXpOBiuxud"', 'defaultMessage:"\u5b9a\u5236",id:"TXpOBiuxud"'),
    ('defaultMessage:"\u81ea\u5b9a\u4e49",id:"TXpOBiuxud"', 'defaultMessage:"\u5b9a\u5236",id:"TXpOBiuxud"'),
    ('defaultMessage:"Connect new sessions to Remote Control",id:"27JV/WKOdz"', 'defaultMessage:"\u65b0\u4f1a\u8bdd\u81ea\u52a8\u8fde\u63a5\u8fdc\u7a0b\u63a7\u5236",id:"27JV/WKOdz"'),
    ('defaultMessage:"Sessions you start on this computer connect to Remote Control automatically, so you can continue them from the terminal or claude.ai/code.",id:"XXiUYflqZf"', 'defaultMessage:"\u5728\u6b64\u7535\u8111\u4e0a\u542f\u52a8\u7684\u4f1a\u8bdd\u5c06\u81ea\u52a8\u8fde\u63a5\u8fdc\u7a0b\u63a7\u5236\uff0c\u4ee5\u4fbf\u4f60\u53ef\u4ee5\u4ece\u7ec8\u7aef\u6216 claude.ai/code \u7ee7\u7eed\u64cd\u4f5c\u3002",id:"XXiUYflqZf"'),
    ('defaultMessage:"Archive inactive sessions",id:"Va2aVQRIyJ"', 'defaultMessage:"\u81ea\u52a8\u5f52\u6863\u95f2\u7f6e\u4f1a\u8bdd",id:"Va2aVQRIyJ"'),
    ('defaultMessage:"Automatically archive local sessions after a period of no activity. Sessions that are running or have background work are never archived, and a worktree with uncommitted changes is kept on disk.",id:"D9OqV7EIr6"', 'defaultMessage:"\u4e00\u6bb5\u65f6\u95f4\u65e0\u6d3b\u52a8\u540e\u81ea\u52a8\u5f52\u6863\u672c\u5730\u4f1a\u8bdd\u3002\u6b63\u5728\u8fd0\u884c\u6216\u6709\u540e\u53f0\u5de5\u4f5c\u7684\u4f1a\u8bdd\u4e0d\u4f1a\u88ab\u5f52\u6863\uff0c\u5305\u542b\u672a\u63d0\u4ea4\u66f4\u6539\u7684\u5de5\u4f5c\u6811\u5c06\u4fdd\u7559\u5728\u78c1\u76d8\u4e0a\u3002",id:"D9OqV7EIr6"'),
    ('defaultMessage:"Inference configuration",id:"+Yw0QZgv1B"', 'defaultMessage:"\u6a21\u578b\u914d\u7f6e",id:"+Yw0QZgv1B"'),
    ('defaultMessage:"Type / for commands",id:"fHClqd2+bS"', 'defaultMessage:"\u6309 / \u6253\u5f00\u547d\u4ee4\u9762\u677f",id:"fHClqd2+bS"'),
    ('defaultMessage:"Mode",id:"mrOnjMzgC4"', 'defaultMessage:"\u6267\u884c\u65b9\u5f0f",id:"mrOnjMzgC4"'),
    ('defaultMessage:"Manual",id:"bEP1fZ8uSe"', 'defaultMessage:"\u6bcf\u6b21\u95ee\u6211",id:"bEP1fZ8uSe"'),
    ('defaultMessage:"Manual permissions",id:"EiPfaLSqUm"', 'defaultMessage:"\u6bcf\u6b21\u95ee\u6211",id:"EiPfaLSqUm"'),
    ('defaultMessage:"Ask permissions",id:"Z/DKbn2gfH"', 'defaultMessage:"\u6bcf\u6b21\u95ee\u6211",id:"Z/DKbn2gfH"'),
    ('defaultMessage:"Manual",id:"W4A9nCednS"', 'defaultMessage:"\u6bcf\u6b21\u95ee\u6211",id:"W4A9nCednS"'),
    ('defaultMessage:"Ask",id:"tpHFrFhksh"', 'defaultMessage:"\u6bcf\u6b21\u95ee\u6211",id:"tpHFrFhksh"'),
    ('defaultMessage:"Ask",id:"ZT0z9z6v6s"', 'defaultMessage:"\u6bcf\u6b21\u95ee\u6211",id:"ZT0z9z6v6s"'),
    ('defaultMessage:"Accept edits",id:"q2/2k27Kto"', 'defaultMessage:"\u81ea\u52a8\u4fee\u6539",id:"q2/2k27Kto"'),
    ('defaultMessage:"Accept",id:"zV6CaeOA6D"', 'defaultMessage:"\u81ea\u52a8\u4fee\u6539",id:"zV6CaeOA6D"'),
    ('defaultMessage:"Plan",id:"6m12Z4ECay"', 'defaultMessage:"\u8ba1\u5212\u6a21\u5f0f",id:"6m12Z4ECay"'),
    ('defaultMessage:"Bypass permissions",id:"q29dVy10XD"', 'defaultMessage:"\u6700\u9ad8\u6743\u9650",id:"q29dVy10XD"'),
    ('defaultMessage:"Bypass permissions",id:"PHx7oHc7eF"', 'defaultMessage:"\u6700\u9ad8\u6743\u9650",id:"PHx7oHc7eF"'),
    ('defaultMessage:"Bypass",id:"OGLnXBWFlh"', 'defaultMessage:"\u6700\u9ad8\u6743\u9650",id:"OGLnXBWFlh"'),
    ('defaultMessage:"Always ask before making changes",id:"uScJvDfyGZ"', 'defaultMessage:"\u6539\u4efb\u4f55\u4e1c\u897f\u524d\u90fd\u5148\u95ee\u4f60",id:"uScJvDfyGZ"'),
    ('defaultMessage:"Claude pauses so you can approve each action.",id:"apqQvFGeSz"', 'defaultMessage:"\u6539\u4efb\u4f55\u4e1c\u897f\u524d\u90fd\u5148\u95ee\u4f60",id:"apqQvFGeSz"'),
    ('defaultMessage:"Automatically accept all file edits",id:"uAswEIOvGS"', 'defaultMessage:"\u81ea\u52a8\u6539\uff0c\u4e0d\u7528\u4f60\u7ba1",id:"uAswEIOvGS"'),
    ('defaultMessage:"Create a plan before making changes",id:"OjKD8Jtn5k"', 'defaultMessage:"\u5148\u5236\u5b9a\u8ba1\u5212\uff0c\u786e\u8ba4\u540e\u518d\u6267\u884c\u4fee\u6539",id:"OjKD8Jtn5k"'),
    ('defaultMessage:"Accepts all permissions",id:"UvugapWhRy"', 'defaultMessage:"Claude \u53ef\u81ea\u52a8\u6267\u884c\u4efb\u4f55\u64cd\u4f5c\uff0c\u4e0d\u518d\u8be2\u95ee",id:"UvugapWhRy"'),
    ('defaultMessage:"Transcript view",id:"sFXw3+Ca5d"', 'defaultMessage:"\u5bf9\u8bdd\u8bb0\u5f55\u89c6\u56fe",id:"sFXw3+Ca5d"'),
    ('defaultMessage:"Fork",id:"SHC19EXDV4"', 'defaultMessage:"\u5206\u652f\u5bf9\u8bdd",id:"SHC19EXDV4"'),
    ('defaultMessage:"Temperature"', 'defaultMessage:"\u968f\u673a\u6027"'),
    ('defaultMessage:"System Prompt"', 'defaultMessage:"\u9884\u8bbe\u6307\u4ee4"'),
    ('label:"Thread"', 'label:"\u5bf9\u8bdd"'),
    ('title:"Thread"', 'title:"\u5bf9\u8bdd"'),
    ('label:"New"', 'label:"\u65b0\u5efa"'),
    ('label:"Projects"', 'label:"\u9879\u76ee"'),
    ('label:"Artifacts"', 'label:"\u4f5c\u54c1"'),
    ('label:"\u5de5\u4ef6"', 'label:"\u4f5c\u54c1"'),
    ('artifactLabel="Artifacts"', 'artifactLabel="\u4f5c\u54c1"'),
    ('artifactLabel="\u5de5\u4ef6"', 'artifactLabel="\u4f5c\u54c1"'),
    ('artifactLabel="\u4eba\u5de5\u5236\u54c1"', 'artifactLabel="\u4f5c\u54c1"'),
    ('artifactLabel="\u5de5\u827a\u54c1"', 'artifactLabel="\u4f5c\u54c1"'),
    ('label:"Scheduled"', 'label:"\u5b9a\u65f6"'),
    ('label:"\u5df2\u5b89\u6392"', 'label:"\u5b9a\u65f6"'),
    ('label:"Customize"', 'label:"\u5b9a\u5236"'),
    ('label:"\u81ea\u5b9a\u4e49"', 'label:"\u5b9a\u5236"'),
    ('["project","\u9879\u76ee"]', '["project","Project"]'),
    ('label:"Live artifacts"', 'label:"\u5b9e\u65f6\u4f5c\u54c1"'),
    ('label:"\u5b9e\u65f6\u5de5\u4ef6"', 'label:"\u5b9e\u65f6\u4f5c\u54c1"'),
    ('label:"\u5b9e\u65f6 Artifacts"', 'label:"\u5b9e\u65f6\u4f5c\u54c1"'),
    ('"Theme"', '"\u4e3b\u9898"'),
    ('"Interface font"', '"\u754c\u9762\u5b57\u4f53"'),
    ('"Font for the Claude Code interface \u2014 menus, sidebar, and chat."', '"Claude Code \u754c\u9762\u5b57\u4f53\uff0c\u7528\u4e8e\u83dc\u5355\u3001\u4fa7\u8fb9\u680f\u548c\u804a\u5929\u3002"'),
    ('"Transcript text size"', '"\u5bf9\u8bdd\u8bb0\u5f55\u6587\u5b57\u5927\u5c0f"'),
    ('"Size of the conversation transcript text."', '"\u5bf9\u8bdd\u8bb0\u5f55\u6587\u5b57\u7684\u5927\u5c0f\u3002"'),
    ('"Code appearance"', '"\u4ee3\u7801\u5916\u89c2"'),
    ('"Set a custom monospace font for code and terminal."', '"\u4e3a\u4ee3\u7801\u548c\u7ec8\u7aef\u8bbe\u7f6e\u81ea\u5b9a\u4e49\u7b49\u5bbd\u5b57\u4f53\u3002"'),
    ('"Local sessions"', '"\u672c\u5730\u4f1a\u8bdd"'),
    ('"Enable remote control by default"', '"\u9ed8\u8ba4\u542f\u7528\u8fdc\u7a0b\u63a7\u5236"'),
    ('"Automatically connect new local sessions to Remote Control so you can continue them from the CLI or claude.ai/code."', '"\u81ea\u52a8\u5c06\u65b0\u7684\u672c\u5730\u4f1a\u8bdd\u8fde\u63a5\u5230\u8fdc\u7a0b\u63a7\u5236\uff0c\u4ee5\u4fbf\u4f60\u53ef\u4ee5\u4ece CLI \u6216 claude.ai/code \u7ee7\u7eed\u4f7f\u7528\u3002"'),
    ('"When Claude pushes changes to a branch, it automatically opens a pull request without asking first. Applies to remote sessions only."', '"Claude \u5c06\u66f4\u6539\u63a8\u9001\u5230\u5206\u652f\u65f6\uff0c\u4f1a\u81ea\u52a8\u6253\u5f00\u62c9\u53d6\u8bf7\u6c42\uff0c\u800c\u4e0d\u4f1a\u5148\u8be2\u95ee\u3002\u4ec5\u9002\u7528\u4e8e\u8fdc\u7a0b\u4f1a\u8bdd\u3002"'),
    ('"Discard Changes"', '"\u653e\u5f03\u66f4\u6539"'),
    ('"Discard changes"', '"\u653e\u5f03\u66f4\u6539"'),
    ('"Discard changes?"', '"\u653e\u5f03\u66f4\u6539\uff1f"'),
    ('"Apply Changes"', '"\u5e94\u7528\u66f4\u6539"'),
    ('"Apply changes"', '"\u5e94\u7528\u66f4\u6539"'),
    ('"Shown in the model picker. Leave blank to auto-format from the ID."', '"\u663e\u793a\u5728\u6a21\u578b\u9009\u62e9\u5668\u4e2d\u3002\u7559\u7a7a\u5c06\u6839\u636e ID \u81ea\u52a8\u683c\u5f0f\u5316\u3002"'),
    ('"Offer 1M-context variant"', '"\u63d0\u4f9b 1M \u4e0a\u4e0b\u6587\u53d8\u4f53"'),
    ('"Model ID"', '"\u6a21\u578b ID"'),
    ('"Allowed surfaces"', '"\u5141\u8bb8\u7684\u529f\u80fd\u754c\u9762"'),
    ('"Enable the Cowork tab. Claude works on longer tasks like research, analysis, and documents."', '"\u542f\u7528 Cowork \u6807\u7b7e\u9875\u3002Claude \u53ef\u5904\u7406\u7814\u7a76\u3001\u5206\u6790\u548c\u6587\u6863\u7b49\u957f\u4efb\u52a1\u3002"'),
    ('"Enable the Code tab. Claude writes and runs code."', '"\u542f\u7528 Code \u6807\u7b7e\u9875\u3002Claude \u53ef\u7f16\u5199\u5e76\u8fd0\u884c\u4ee3\u7801\u3002"'),
    ('"General restrictions"', '"\u901a\u7528\u9650\u5236"'),
    ('"These apply regardless of which surfaces are enabled."', '"\u65e0\u8bba\u542f\u7528\u54ea\u4e9b\u529f\u80fd\u754c\u9762\uff0c\u8fd9\u4e9b\u9650\u5236\u90fd\u4f1a\u751f\u6548\u3002"'),
    ('"Hostnames the agent\'s tools may reach from the Cowork and Code tabs. Also surfaced under Egress Requirements."', '"Agent \u5de5\u5177\u53ef\u4ece Cowork \u548c Code \u6807\u7b7e\u9875\u8bbf\u95ee\u7684\u4e3b\u673a\u540d\u3002\u4e5f\u4f1a\u663e\u793a\u5728\u51fa\u53e3\u8981\u6c42\u4e2d\u3002"'),
    ('"Applies to both the Cowork and Code tabs."', '"\u540c\u65f6\u9002\u7528\u4e8e Cowork \u548c Code \u6807\u7b7e\u9875\u3002"'),
    ('Applies to both the Cowork and Code tabs.', '\u540c\u65f6\u9002\u7528\u4e8e Cowork \u548c Code \u6807\u7b7e\u9875\u3002'),
    ('"Only affects **tool calls**. Inference and MCP traffic are covered by their own allowlists elsewhere."', '"\u4ec5\u5f71\u54cd **\u5de5\u5177\u8c03\u7528**\u3002\u63a8\u7406\u548c MCP \u6d41\u91cf\u7531\u5176\u4ed6\u4f4d\u7f6e\u7684\u5141\u8bb8\u5217\u8868\u63a7\u5236\u3002"'),
    ('Only affects **tool calls**. Inference and MCP traffic are covered by their own allowlists elsewhere.', '\u4ec5\u5f71\u54cd **\u5de5\u5177\u8c03\u7528**\u3002\u63a8\u7406\u548c MCP \u6d41\u91cf\u7531\u5176\u4ed6\u4f4d\u7f6e\u7684\u5141\u8bb8\u5217\u8868\u63a7\u5236\u3002'),
    ('"When unset, only the inference endpoint is reachable from the sandbox; the agent\'s package installs (pip/npm) and web fetches will fail with a 403."', '"\u672a\u8bbe\u7f6e\u65f6\uff0c\u6c99\u76d2\u53ea\u80fd\u8bbf\u95ee\u63a8\u7406\u7aef\u70b9\uff1bAgent \u7684\u5305\u5b89\u88c5\uff08pip/npm\uff09\u548c\u7f51\u9875\u6293\u53d6\u5c06\u56e0 403 \u5931\u8d25\u3002"'),
    ('When unset, only the inference endpoint is reachable from the sandbox; the agent\'s package installs (pip/npm) and web fetches will fail with a 403.', '\u672a\u8bbe\u7f6e\u65f6\uff0c\u6c99\u76d2\u53ea\u80fd\u8bbf\u95ee\u63a8\u7406\u7aef\u70b9\uff1bAgent \u7684\u5305\u5b89\u88c5\uff08pip/npm\uff09\u548c\u7f51\u9875\u6293\u53d6\u5c06\u56e0 403 \u5931\u8d25\u3002'),
    ('"Accepts exact hostnames (`api.github.com`), wildcards (`*.corp.com` matches one subdomain level), and `*` to allow all."', '"\u63a5\u53d7\u7cbe\u786e\u4e3b\u673a\u540d\uff08`api.github.com`\uff09\u3001\u901a\u914d\u7b26\uff08`*.corp.com` \u5339\u914d\u4e00\u7ea7\u5b50\u57df\uff09\u548c `*`\uff08\u5141\u8bb8\u5168\u90e8\uff09\u3002"'),
    ('Accepts exact hostnames (`api.github.com`), wildcards (`*.corp.com` matches one subdomain level), and `*` to allow all.', '\u63a5\u53d7\u7cbe\u786e\u4e3b\u673a\u540d\uff08`api.github.com`\uff09\u3001\u901a\u914d\u7b26\uff08`*.corp.com` \u5339\u914d\u4e00\u7ea7\u5b50\u57df\uff09\u548c `*`\uff08\u5141\u8bb8\u5168\u90e8\uff09\u3002'),
    ('"Wildcards don\'t cross schemes. `*.corp.com` matches `docs.corp.com` but not `corp.com` itself; add both if you need the apex."', '"\u901a\u914d\u7b26\u4e0d\u8de8\u8d8a\u5c42\u7ea7\u3002`*.corp.com` \u5339\u914d `docs.corp.com`\uff0c\u4e0d\u5339\u914d `corp.com` \u672c\u8eab\uff1b\u9700\u8981\u9876\u7ea7\u57df\u65f6\u8bf7\u540c\u65f6\u6dfb\u52a0\u4e24\u8005\u3002"'),
    ('Wildcards don\'t cross schemes. `*.corp.com` matches `docs.corp.com` but not `corp.com` itself; add both if you need the apex.', '\u901a\u914d\u7b26\u4e0d\u8de8\u8d8a\u5c42\u7ea7\u3002`*.corp.com` \u5339\u914d `docs.corp.com`\uff0c\u4e0d\u5339\u914d `corp.com` \u672c\u8eab\uff1b\u9700\u8981\u9876\u7ea7\u57df\u65f6\u8bf7\u540c\u65f6\u6dfb\u52a0\u4e24\u8005\u3002'),
    ('"IP literals and localhost always resolve regardless of this list; this is a public-egress filter, not a sandbox."', '"IP \u5b57\u9762\u91cf\u548c localhost \u59cb\u7ec8\u53ef\u89e3\u6790\uff0c\u4e0d\u53d7\u6b64\u5217\u8868\u5f71\u54cd\uff1b\u8fd9\u662f\u516c\u5171\u51fa\u7ad9\u8fc7\u6ee4\u5668\uff0c\u4e0d\u662f\u6c99\u76d2\u3002"'),
    ('IP literals and localhost always resolve regardless of this list; this is a public-egress filter, not a sandbox.', 'IP \u5b57\u9762\u91cf\u548c localhost \u59cb\u7ec8\u53ef\u89e3\u6790\uff0c\u4e0d\u53d7\u6b64\u5217\u8868\u5f71\u54cd\uff1b\u8fd9\u662f\u516c\u5171\u51fa\u7ad9\u8fc7\u6ee4\u5668\uff0c\u4e0d\u662f\u6c99\u76d2\u3002'),
    ('"Hosts you add here also need to be open on your network firewall. See Egress Requirements for the full allowlist."', '"\u4f60\u5728\u6b64\u6dfb\u52a0\u7684\u4e3b\u673a\u4e5f\u9700\u5728\u7f51\u7edc\u9632\u706b\u5899\u4e0a\u5f00\u653e\u3002\u5b8c\u6574\u5141\u8bb8\u5217\u8868\u8bf7\u67e5\u770b\u51fa\u53e3\u8981\u6c42\u3002"'),
    ('Hosts you add here also need to be open on your network firewall. See Egress Requirements for the full allowlist.', '\u4f60\u5728\u6b64\u6dfb\u52a0\u7684\u4e3b\u673a\u4e5f\u9700\u5728\u7f51\u7edc\u9632\u706b\u5899\u4e0a\u5f00\u653e\u3002\u5b8c\u6574\u5141\u8bb8\u5217\u8868\u8bf7\u67e5\u770b\u51fa\u53e3\u8981\u6c42\u3002'),
    ('"Discard unsaved changes?"', '"\u653e\u5f03\u672a\u4fdd\u5b58\u7684\u66f4\u6539\uff1f"'),
    ('"This configuration has changes that haven\'t been saved. They will be lost."', '"\u6b64\u914d\u7f6e\u6709\u672a\u4fdd\u5b58\u7684\u66f4\u6539\u3002\u8fd9\u4e9b\u66f4\u6539\u5c06\u4e22\u5931\u3002"'),
    ('"Keep editing"', '"\u7ee7\u7eed\u7f16\u8f91"'),
    ('defaultMessage:"Discard",id:"nmpevlUATU"', 'defaultMessage:"\u653e\u5f03",id:"nmpevlUATU"'),
    ('"High-contrast dark theme"', '"\u9ad8\u5bf9\u6bd4\u5ea6\u6df1\u8272\u4e3b\u9898"'),
    ('"Use a darker, near-black background when dark mode is on."', '"\u6df1\u8272\u6a21\u5f0f\u5f00\u542f\u65f6\u4f7f\u7528\u66f4\u6df1\u3001\u63a5\u8fd1\u7eaf\u9ed1\u7684\u80cc\u666f\u3002"'),
    ('defaultMessage:"Small",id:"BPnT3TVya+"', 'defaultMessage:"\u5c0f",id:"BPnT3TVya+"'),
    ('defaultMessage:"Medium",id:"ovJ26CKo4Q"', 'defaultMessage:"\u4e2d",id:"ovJ26CKo4Q"'),
    ('defaultMessage:"Large",id:"/06iwcQHPz"', 'defaultMessage:"\u5927",id:"/06iwcQHPz"'),
    ('"Dynamic workflows"', '"\u52a8\u6001\u5de5\u4f5c\u6d41"'),
    ('"Let Claude run multiple agents in parallel for complex tasks. Workflows can use a lot of your usage limit quickly."', '"\u5141\u8bb8 Claude \u4e3a\u590d\u6742\u4efb\u52a1\u5e76\u884c\u8fd0\u884c\u591a\u4e2a Agent\u3002\u5de5\u4f5c\u6d41\u53ef\u80fd\u5f88\u5feb\u6d88\u8017\u4f60\u7684\u4f7f\u7528\u989d\u5ea6\u3002"'),
    ('"Dynamic workflows run many subagents in parallel and can use a lot of your usage limit. Stop them any time from the <link>tasks panel</link>."', '"\u52a8\u6001\u5de5\u4f5c\u6d41\u4f1a\u5e76\u884c\u8fd0\u884c\u591a\u4e2a\u5b50 Agent\uff0c\u5e76\u53ef\u80fd\u6d88\u8017\u5927\u91cf\u4f7f\u7528\u989d\u5ea6\u3002\u4f60\u53ef\u968f\u65f6\u4ece<link>\u4efb\u52a1\u9762\u677f</link>\u505c\u6b62\u5b83\u4eec\u3002"'),
    ('"Dynamic workflows are disabled by your organization\'s policy."', '"\u52a8\u6001\u5de5\u4f5c\u6d41\u5df2\u88ab\u4f60\u7684\u7ec4\u7ec7\u7b56\u7565\u7981\u7528\u3002"'),
    ('"Cowork files"', '"Cowork \u6587\u4ef6"'),
    ('"Your artifacts and scheduled tasks are stored at {path}."', '"\u4f60\u7684\u4f5c\u54c1\u548c\u8ba1\u5212\u4efb\u52a1\u5b58\u50a8\u5728 {path}\u3002"'),
    ('"Change location for Cowork files?"', '"\u66f4\u6539 Cowork \u6587\u4ef6\u4f4d\u7f6e\uff1f"'),
    ('"Copy files to {location} and restart the app. Your existing files will remain in {previousLocation}."', '"\u5c06\u6587\u4ef6\u590d\u5236\u5230 {location} \u5e76\u91cd\u542f\u5e94\u7528\u3002\u4f60\u7684\u73b0\u6709\u6587\u4ef6\u5c06\u4fdd\u7559\u5728 {previousLocation}\u3002"'),
    ('"{provider} returned an error"', '"{provider} \u8fd4\u56de\u9519\u8bef"'),
    ('"Your connection works, but the provider rejected a test request. Often a model-access or quota issue."', '"\u8fde\u63a5\u6b63\u5e38\uff0c\u4f46\u63d0\u4f9b\u5546\u62d2\u7edd\u4e86\u6d4b\u8bd5\u8bf7\u6c42\u3002\u8fd9\u901a\u5e38\u662f\u6a21\u578b\u8bbf\u95ee\u6743\u9650\u6216\u914d\u989d\u95ee\u9898\u3002"'),
    ('"Connectors have moved to Customize. Head there to browse, connect, and manage them."', '"\u8fde\u63a5\u5668\u5df2\u79fb\u81f3\u201c\u81ea\u5b9a\u4e49\u201d\u3002\u524d\u5f80\u90a3\u91cc\u6d4f\u89c8\u3001\u8fde\u63a5\u548c\u7ba1\u7406\u8fde\u63a5\u5668\u3002"'),
    ('Connectors have moved to Customize. Head there to browse, connect, and manage them.', '\u8fde\u63a5\u5668\u5df2\u79fb\u81f3\u201c\u81ea\u5b9a\u4e49\u201d\u3002\u524d\u5f80\u90a3\u91cc\u6d4f\u89c8\u3001\u8fde\u63a5\u548c\u7ba1\u7406\u8fde\u63a5\u5668\u3002'),
    ('"Connectors have moved to <link>Customize</link>. Head there to browse, connect, and manage them."', '"\u8fde\u63a5\u5668\u5df2\u79fb\u81f3<link>\u81ea\u5b9a\u4e49</link>\u3002\u524d\u5f80\u90a3\u91cc\u6d4f\u89c8\u3001\u8fde\u63a5\u548c\u7ba1\u7406\u8fde\u63a5\u5668\u3002"'),
    ('"Skills have moved to Customize."', '"\u6280\u80fd\u5df2\u79fb\u81f3\u201c\u81ea\u5b9a\u4e49\u201d\u3002"'),
    ('Skills have moved to Customize.', '\u6280\u80fd\u5df2\u79fb\u81f3\u201c\u81ea\u5b9a\u4e49\u201d\u3002'),
    ('"Skills have moved to <link>Customize</link>."', '"\u6280\u80fd\u5df2\u79fb\u81f3<link>\u81ea\u5b9a\u4e49</link>\u3002"'),
    ('"Generate code, documents, and designs in a dedicated window alongside your conversation."', '"\u5728\u5bf9\u8bdd\u65c1\u7684\u4e13\u7528\u7a97\u53e3\u4e2d\u751f\u6210\u4ee3\u7801\u3001\u6587\u6863\u548c\u8bbe\u8ba1\u3002"'),
    ('Generate code, documents, and designs in a dedicated window alongside your conversation.', '\u5728\u5bf9\u8bdd\u65c1\u7684\u4e13\u7528\u7a97\u53e3\u4e2d\u751f\u6210\u4ee3\u7801\u3001\u6587\u6863\u548c\u8bbe\u8ba1\u3002'),
    ('"Create dynamic artifacts that stay up-to-date using live data from your connectors."', '"\u4f7f\u7528\u6765\u81ea\u8fde\u63a5\u5668\u7684\u5b9e\u65f6\u6570\u636e\uff0c\u521b\u5efa\u4fdd\u6301\u66f4\u65b0\u7684\u52a8\u6001\u4f5c\u54c1\u3002"'),
    ('Create dynamic artifacts that stay up-to-date using live data from your connectors.', '\u4f7f\u7528\u6765\u81ea\u8fde\u63a5\u5668\u7684\u5b9e\u65f6\u6570\u636e\uff0c\u521b\u5efa\u4fdd\u6301\u66f4\u65b0\u7684\u52a8\u6001\u4f5c\u54c1\u3002'),
    ('"Create dynamic artifacts that stay up-to-date using live data from <link>your connectors</link>."', '"\u4f7f\u7528\u6765\u81ea<link>\u4f60\u7684\u8fde\u63a5\u5668</link>\u7684\u5b9e\u65f6\u6570\u636e\uff0c\u521b\u5efa\u4fdd\u6301\u66f4\u65b0\u7684\u52a8\u6001\u4f5c\u54c1\u3002"'),
    ('"Claude will keep these in mind across chats and Cowork within <aupLink>Anthropic\'s guidelines</aupLink>. <learnMoreLink>Learn more</learnMoreLink>"', '"Claude \u4f1a\u5728\u804a\u5929\u548c Cowork \u4e2d\u8bb0\u4f4f\u8fd9\u4e9b\u5185\u5bb9\uff0c\u5e76\u9075\u5faa<aupLink>Anthropic \u7684\u6307\u5357</aupLink>\u3002<learnMoreLink>\u4e86\u89e3\u66f4\u591a</learnMoreLink>"'),
    ('Claude will keep these in mind across chats and Cowork within <aupLink>Anthropic\'s guidelines</aupLink>. <learnMoreLink>Learn more</learnMoreLink>', 'Claude \u4f1a\u5728\u804a\u5929\u548c Cowork \u4e2d\u8bb0\u4f4f\u8fd9\u4e9b\u5185\u5bb9\uff0c\u5e76\u9075\u5faa<aupLink>Anthropic \u7684\u6307\u5357</aupLink>\u3002<learnMoreLink>\u4e86\u89e3\u66f4\u591a</learnMoreLink>'),
    ('"Claude will keep these in mind across chats and Cowork within Anthropic\'s guidelines. Learn more"', '"Claude \u4f1a\u5728\u804a\u5929\u548c Cowork \u4e2d\u8bb0\u4f4f\u8fd9\u4e9b\u5185\u5bb9\uff0c\u5e76\u9075\u5faa Anthropic \u7684\u6307\u5357\u3002\u4e86\u89e3\u66f4\u591a"'),
    ('Claude will keep these in mind across chats and Cowork within Anthropic\'s guidelines. Learn more', 'Claude \u4f1a\u5728\u804a\u5929\u548c Cowork \u4e2d\u8bb0\u4f4f\u8fd9\u4e9b\u5185\u5bb9\uff0c\u5e76\u9075\u5faa Anthropic \u7684\u6307\u5357\u3002\u4e86\u89e3\u66f4\u591a'),
    ('"Claude will keep these in mind across chats and Cowork within Anthropic\u2019s guidelines. Learn more"', '"Claude \u4f1a\u5728\u804a\u5929\u548c Cowork \u4e2d\u8bb0\u4f4f\u8fd9\u4e9b\u5185\u5bb9\uff0c\u5e76\u9075\u5faa Anthropic \u7684\u6307\u5357\u3002\u4e86\u89e3\u66f4\u591a"'),
    ('Claude will keep these in mind across chats and Cowork within Anthropic\u2019s guidelines. Learn more', 'Claude \u4f1a\u5728\u804a\u5929\u548c Cowork \u4e2d\u8bb0\u4f4f\u8fd9\u4e9b\u5185\u5bb9\uff0c\u5e76\u9075\u5faa Anthropic \u7684\u6307\u5357\u3002\u4e86\u89e3\u66f4\u591a'),
    ('"Configured model not available"', '"\u914d\u7f6e\u7684\u6a21\u578b\u4e0d\u53ef\u7528"'),
    ('"Your gateway couldn\'t serve {model}. This model may not be configured on your gateway, or access may be restricted."', '"\u7b2c\u4e09\u65b9\u65e0\u6cd5\u63d0\u4f9b {model}\u3002\u8be5\u6a21\u578b\u53ef\u80fd\u672a\u5728\u7b2c\u4e09\u65b9\u914d\u7f6e\uff0c\u6216\u8bbf\u95ee\u53d7\u9650\u3002"'),
    ('"Your gateway couldn\u2019t serve {model}. This model may not be configured on your gateway, or access may be restricted."', '"\u7b2c\u4e09\u65b9\u65e0\u6cd5\u63d0\u4f9b {model}\u3002\u8be5\u6a21\u578b\u53ef\u80fd\u672a\u5728\u7b2c\u4e09\u65b9\u914d\u7f6e\uff0c\u6216\u8bbf\u95ee\u53d7\u9650\u3002"'),
    ('"Gateway base URL"', '"\u7b2c\u4e09\u65b9 URL"'),
    ('"Gateway API key"', '"\u7b2c\u4e09\u65b9 API Key"'),
    ('"Gateway auth scheme"', '"\u7b2c\u4e09\u65b9\u8ba4\u8bc1\u65b9\u5f0f"'),
    ('"Gateway extra headers"', '"\u7b2c\u4e09\u65b9\u989d\u5916\u8bf7\u6c42\u5934"'),
    ('"Gateway SSO IdP (OIDC)"', '"\u7b2c\u4e09\u65b9 SSO IdP (OIDC)"'),
    ('"Gateway sign-in (OIDC)"', '"\u7b2c\u4e09\u65b9\u767b\u5f55 (OIDC)"'),
    ('"Interactive sign-in"', '"\u4ea4\u4e92\u5f0f\u767b\u5f55"'),
    ('"Helper script"', '"\u8f85\u52a9\u811a\u672c"'),
    ('"Link URL"', '"\u94fe\u63a5 URL"'),
    ('"Optional HTTPS URL. The banner text becomes a link when set."', '"\u53ef\u9009 HTTPS URL\u3002\u8bbe\u7f6e\u540e\uff0c\u6a2a\u5e45\u6587\u672c\u4f1a\u53d8\u6210\u94fe\u63a5\u3002"'),
    ('"Show banner"', '"\u663e\u793a\u6a2a\u5e45"'),
    ('"Banner text"', '"\u6a2a\u5e45\u6587\u672c"'),
    ('"Single line, truncated on overflow. Maximum 200 characters."', '"\u5355\u884c\u663e\u793a\uff0c\u6ea2\u51fa\u65f6\u622a\u65ad\u3002\u6700\u591a 200 \u4e2a\u5b57\u7b26\u3002"'),
    ('"Internal use only"', '"\u4ec5\u4f9b\u5185\u90e8\u4f7f\u7528"'),
    ('"Background color"', '"\u80cc\u666f\u989c\u8272"'),
    ('"Six-digit hex (#RRGGBB). Applied exactly as configured; not theme-adapted."', '"\u516d\u4f4d\u5341\u516d\u8fdb\u5236\u989c\u8272\uff08#RRGGBB\uff09\u3002\u5c06\u5b8c\u5168\u6309\u914d\u7f6e\u5e94\u7528\uff0c\u4e0d\u4f1a\u9002\u914d\u4e3b\u9898\u3002"'),
    ('"Text color"', '"\u6587\u672c\u989c\u8272"'),
    ('"Appearance"', '"\u7ec4\u7ec7\u6a2a\u5e45"'),
    ('"MCP servers"', '"MCP \u670d\u52a1\u5668"'),
    ('"Blank"', '"\u7a7a\u767d"'),
    ('"Microsoft365"', '"Microsoft 365"'),
    ('"OpenTelemetry"', '"OpenTelemetry"'),
    ('"OpenTelemetry collector endpoint"', '"OpenTelemetry \u91c7\u96c6\u5668\u7aef\u70b9"'),
    ('"OpenTelemetry exporter headers"', '"OpenTelemetry \u5bfc\u51fa\u5668\u6807\u5934"'),
    ('"Telemetry config conflict"', '"\u9065\u6d4b\u914d\u7f6e\u51b2\u7a81"'),
    ('"Local MCP servers"', '"\u672c\u5730 MCP \u670d\u52a1\u5668"'),
    ('"Known MCP servers"', '"\u5df2\u77e5 MCP \u670d\u52a1\u5668"'),
    ('"Your MCP servers"', '"\u4f60\u7684 MCP \u670d\u52a1\u5668"'),
    ('"Usage limits"', '"\u4f7f\u7528\u9650\u5236"'),
    ('"Max tokens per window"', '"\u6bcf\u4e2a\u7a97\u53e3\u7684\u6700\u5927\u4ee4\u724c\u6570"'),
    ('"Require signed extensions"', '"\u9700\u8981\u7b7e\u540d\u7684\u6269\u5c55"'),
    ('"Allow Claude Code tab"', '"\u5141\u8bb8 Claude Code \u9009\u9879\u5361"'),
    ('"No organization plugins found"', '"\u6ca1\u6709\u7ec4\u7ec7\u63d2\u4ef6"'),
    ('"Add desktop extensions"', '"\u6dfb\u52a0\u684c\u9762\u6269\u5c55"'),
    ('"Link URL (optional)"', '"\u94fe\u63a5 URL\uff08\u53ef\u9009\uff09"'),
    ('"Headers helper script"', '"Headers \u8f85\u52a9\u811a\u672c"'),
    ('"Absolute path"', '"\u7edd\u5bf9\u8def\u5f84"'),
    ('"Tool policy"', '"\u5de5\u5177\u7b56\u7565"'),
    ('"Lock the approval state for specific tools. Unlisted tools stay user-controlled."', '"\u9501\u5b9a\u7279\u5b9a\u5de5\u5177\u7684\u5ba1\u6279\u72b6\u6001\u3002\u672a\u5217\u51fa\u7684\u5de5\u5177\u4ecd\u7531\u7528\u6237\u63a7\u5236\u3002"'),
    ('"Auto-register (dynamic client registration)"', '"\u81ea\u52a8\u6ce8\u518c\uff08\u52a8\u6001\u5ba2\u6237\u7aef\u6ce8\u518c\uff09"'),
    ('"Bring your own client"', '"\u4f7f\u7528\u81ea\u5df1\u7684\u5ba2\u6237\u7aef"'),
    ('"Streamable HTTP"', '"\u6d41\u5f0f HTTP"'),
    ('"SSE (legacy)"', '"SSE\uff08\u65e7\u7248\uff09"'),
    ('"Local command (stdio)"', '"\u672c\u5730\u547d\u4ee4\uff08stdio\uff09"'),
    ('"Item 1"', '"\u9879 1"'),
    ('"Invalid configuration"', '"\u65e0\u6548\u914d\u7f6e"'),
    ('"Hi, I\'m Claude. How can I help you today?"', '"\u4f60\u597d\uff0c\u6211\u662f Claude\u3002\u4eca\u5929\u6211\u80fd\u5e2e\u4f60\u4ec0\u4e48\uff1f"'),
    ('"Hi, I\u2019m Claude. How can I help you today?"', '"\u4f60\u597d\uff0c\u6211\u662f Claude\u3002\u4eca\u5929\u6211\u80fd\u5e2e\u4f60\u4ec0\u4e48\uff1f"'),
    ('"Hi, I\'m Claude. How can I helpyou today?"', '"\u4f60\u597d\uff0c\u6211\u662f Claude\u3002\u4eca\u5929\u6211\u80fd\u5e2e\u4f60\u4ec0\u4e48\uff1f"'),
    ('"Hi, I\u2019m Claude. How can I helpyou today?"', '"\u4f60\u597d\uff0c\u6211\u662f Claude\u3002\u4eca\u5929\u6211\u80fd\u5e2e\u4f60\u4ec0\u4e48\uff1f"'),
    ('"Allowed egress hosts"', '"\u5141\u8bb8\u7684\u51fa\u7ad9\u4e3b\u673a"'),
    ('"Disable claude:// deep-link handling"', '"\u7981\u7528 claude:// \u6df1\u5ea6\u94fe\u63a5\u5904\u7406"'),
    ('"Enable Main Process Debugger"', '"\u542f\u7528\u4e3b\u8fdb\u7a0b\u8c03\u8bd5\u5668"'),
    ('"Record Performance Trace"', '"\u8bb0\u5f55\u6027\u80fd\u8ddf\u8e2a"'),
    ('"Write Main Process Heap Snapshot"', '"\u5199\u5165\u4e3b\u8fdb\u7a0b\u5806\u5feb\u7167"'),
    ('"Record Memory Trace (auto-stop)"', '"\u8bb0\u5f55\u5185\u5b58\u8ddf\u8e2a\uff08\u81ea\u52a8\u505c\u6b62\uff09"'),
    ('"server-name"', '"\u670d\u52a1\u5668\u540d\u79f0"'),
    ('"tool name"', '"\u5de5\u5177\u540d\u79f0"'),
    ('"+ Add server policy"', '"+ \u6dfb\u52a0\u670d\u52a1\u5668\u7b56\u7565"'),
    ('"Open Setup"', '"\u6253\u5f00\u8bbe\u7f6e\u5411\u5bfc"'),
    ('"Sort by"', '"\u6392\u5e8f\u65b9\u5f0f"'),
    ('"Recency"', '"\u6700\u8fd1"'),
    ('"Alphabetically"', '"\u6309\u5b57\u6bcd\u987a\u5e8f"'),
    ('"Created time"', '"\u521b\u5efa\u65f6\u95f4"'),
    ('"Custom groups"', '"\u81ea\u5b9a\u4e49\u5206\u7ec4"'),
    ('"Avatar"', '"\u5934\u50cf"'),
    ('"Instructions for Claude"', '"\u7ed9 Claude \u7684\u6307\u4ee4"'),
    ('"Preferences"', '"\u504f\u597d\u8bbe\u7f6e"'),
    ('"Get notified when Claude has finished a response. Useful for long-running tasks."', '"Claude \u5b8c\u6210\u56de\u590d\u65f6\u63a5\u6536\u901a\u77e5\u3002\u5bf9\u957f\u65f6\u95f4\u8fd0\u884c\u7684\u4efb\u52a1\u5f88\u6709\u7528\u3002"'),
    ('"You\u2019re running Claude through your organization\u2019s own inference provider (cc.freemodel.dev). Your conversations are sent there, not to Anthropic, and are governed by your organization\u2019s agreement with that provider."', '"\u4f60\u6b63\u5728\u901a\u8fc7\u7ec4\u7ec7\u81ea\u5df1\u7684\u63a8\u7406\u63d0\u4f9b\u65b9 (cc.freemodel.dev) \u8fd0\u884c Claude\u3002\u4f60\u7684\u5bf9\u8bdd\u4f1a\u53d1\u9001\u5230\u8be5\u63d0\u4f9b\u65b9\uff0c\u800c\u4e0d\u662f Anthropic\uff0c\u5e76\u53d7\u4f60\u7684\u7ec4\u7ec7\u4e0e\u8be5\u63d0\u4f9b\u65b9\u4e4b\u95f4\u534f\u8bae\u7684\u7ea6\u675f\u3002"'),
    ('You\u2019re running Claude through your organization\u2019s own inference provider (cc.freemodel.dev). Your conversations are sent there, not to Anthropic, and are governed by your organization\u2019s agreement with that provider.', '\u4f60\u6b63\u5728\u901a\u8fc7\u7ec4\u7ec7\u81ea\u5df1\u7684\u63a8\u7406\u63d0\u4f9b\u65b9 (cc.freemodel.dev) \u8fd0\u884c Claude\u3002\u4f60\u7684\u5bf9\u8bdd\u4f1a\u53d1\u9001\u5230\u8be5\u63d0\u4f9b\u65b9\uff0c\u800c\u4e0d\u662f Anthropic\uff0c\u5e76\u53d7\u4f60\u7684\u7ec4\u7ec7\u4e0e\u8be5\u63d0\u4f9b\u65b9\u4e4b\u95f4\u534f\u8bae\u7684\u7ea6\u675f\u3002'),
    ('"What Anthropic doesn\u2019t see"', '"Anthropic \u4e0d\u4f1a\u770b\u5230\u7684\u5185\u5bb9"'),
    ('"Your prompts, Claude\u2019s responses, or any conversation content"', '"\u4f60\u7684\u63d0\u793a\u3001Claude \u7684\u56de\u590d\u6216\u4efb\u4f55\u5bf9\u8bdd\u5185\u5bb9"'),
    ('"Your files, code, or workspace contents"', '"\u4f60\u7684\u6587\u4ef6\u3001\u4ee3\u7801\u6216\u5de5\u4f5c\u533a\u5185\u5bb9"'),
    ('"Your identity or account details"', '"\u4f60\u7684\u8eab\u4efd\u6216\u8d26\u53f7\u8be6\u60c5"'),
    ('"What Anthropic may receive (configured by your organization)"', '"Anthropic \u53ef\u80fd\u6536\u5230\u7684\u5185\u5bb9\uff08\u7531\u4f60\u7684\u7ec4\u7ec7\u914d\u7f6e\uff09"'),
    ('"Crash reports and error diagnostics, so we can fix bugs"', '"\u5d29\u6e83\u62a5\u544a\u548c\u9519\u8bef\u8bca\u65ad\uff0c\u7528\u4e8e\u4fee\u590d\u95ee\u9898"'),
    ('"Anonymous usage metrics including usage counts (not conversation content)"', '"\u5305\u542b\u4f7f\u7528\u6b21\u6570\u7684\u533f\u540d\u4f7f\u7528\u6307\u6807\uff08\u4e0d\u5305\u542b\u5bf9\u8bdd\u5185\u5bb9\uff09"'),
    ('"Update-check requests, so the app can stay current"', '"\u66f4\u65b0\u68c0\u67e5\u8bf7\u6c42\uff0c\u7528\u4e8e\u4fdd\u6301\u5e94\u7528\u4e3a\u6700\u65b0\u7248\u672c"'),
    ('"A diagnostic report, only if you explicitly choose \u201cSend to Anthropic\u201d"', '"\u8bca\u65ad\u62a5\u544a\uff0c\u4ec5\u5f53\u4f60\u660e\u786e\u9009\u62e9\u201c\u53d1\u9001\u7ed9 Anthropic\u201d\u65f6\u624d\u4f1a\u53d1\u9001"'),
    ('"New task"', '"\u65b0\u5efa\u4efb\u52a1"'),
    ('"New session"', '"\u65b0\u5efa\u4f1a\u8bdd"'),
    ('"New Projects"', '"\u65b0\u5efa\u9879\u76ee"'),
    ('"Scheduled"', '"\u5b9a\u65f6"'),
    ('"Status"', '"\u72b6\u6001"'),
    ('"Last activity"', '"\u6700\u8fd1\u6d3b\u52a8"'),
    ('"Group by"', '"\u5206\u7ec4\u65b9\u5f0f"'),
    ('"Date"', '"\u65e5\u671f"'),
    ('"Custom groups"', '"\u81ea\u5b9a\u4e49\u5206\u7ec4"'),
    ('"None"', '"\u65e0"'),
    ('"1d"', '"1\u5929"'),
    ('"3d"', '"3\u5929"'),
    ('"7d"', '"7\u5929"'),
    ('"30d"', '"30\u5929"'),
    ('"All projects"', '"\u6240\u6709\u9879\u76ee"'),
    ('"View all"', '"\u67e5\u770b\u5168\u90e8"'),
    ('"Viewall"', '"\u67e5\u770b\u5168\u90e8"'),
    ('"Create your first scheduled task"', '"\u521b\u5efa\u4f60\u7684\u7b2c\u4e00\u4e2a\u8ba1\u5212\u4efb\u52a1"'),
    ('"Daily brief"', '"\u6bcf\u65e5\u7b80\u62a5"'),
    ('"Weekly review"', '"\u6bcf\u5468\u56de\u987e"'),
    ('"Run tasks on a schedule or whenever you need them. Type /schedule in any existing task to set one up."', '"\u6309\u8ba1\u5212\u6216\u5728\u9700\u8981\u65f6\u8fd0\u884c\u4efb\u52a1\u3002\u5728\u4efb\u4f55\u73b0\u6709\u4efb\u52a1\u4e2d\u8f93\u5165 /schedule \u5373\u53ef\u8bbe\u7f6e\u3002"'),
    ('label:"Tasks"', 'label:"\u4efb\u52a1"'),
    ('"Active"', '"\u6d3b\u8dc3"'),
    ('"Archived"', '"\u5df2\u5f52\u6863"'),
    ('"All"', '"\u5168\u90e8"'),
    ('"Local"', '"\u672c\u5730"'),
    ('label:"Cloud"', 'label:"\u4e91\u7aef"'),
    ('"Environment"', '"\u73af\u5883"'),
    ('"Recents"', '"\u6700\u8fd1"'),
]

# === Sidebar navigation (cbc59a8af-DbOQVv5S.js) ===
PATCHES["cbc59a8af-DbOQVv5S.js"] = [
    ('label:"\u804a\u5929"', 'label:"\u804a\u5929"'),
    ('label:"Chat"', 'label:"\u804a\u5929"'),
    ('label:"Cowork"', 'label:"\u529e\u516c"'),
    ('label:"\u534f\u4f5c"', 'label:"\u529e\u516c"'),
    ('label:"Code"', 'label:"\u7f16\u7801"'),
    ('label:"\u4ee3\u7801"', 'label:"\u7f16\u7801"'),
    ('label:"Operon"', 'label:"\u5b9e\u9a8c\u5ba4"'),
    ('label:"New"', 'label:"\u65b0\u5efa"'),
    ('label:"Projects"', 'label:"\u9879\u76ee"'),
    ('label:"Artifacts"', 'label:"\u4f5c\u54c1"'),
    ('label:"\u5de5\u4ef6"', 'label:"\u4f5c\u54c1"'),
    ('label:"\u5df2\u5b89\u6392"', 'label:"\u5b9a\u65f6"'),
    ('label:"Scheduled"', 'label:"\u5b9a\u65f6"'),
    ('label:"\u4efb\u52a1"', 'label:"\u4efb\u52a1"'),
    ('label:"Tasks"', 'label:"\u4efb\u52a1"'),
    ('label:"Pull Requests"', 'label:"\u62c9\u53d6\u8bf7\u6c42"'),
    ('label:"\u56de\u653e"', 'label:"\u56de\u653e"'),
    ('label:"Replay"', 'label:"\u56de\u653e"'),
    ('label:"\u8c03\u5ea6"', 'label:"\u8c03\u5ea6"'),
    ('label:"Dispatch"', 'label:"\u8c03\u5ea6"'),
    ('label:"\u60f3\u6cd5"', 'label:"\u60f3\u6cd5"'),
    ('label:"Ideas"', 'label:"\u60f3\u6cd5"'),
    ('label:"\u5e94\u7528"', 'label:"\u5e94\u7528"'),
    ('label:"Apps"', 'label:"\u5e94\u7528"'),
    ('label:"\u5b89\u5168"', 'label:"\u5b89\u5168"'),
    ('label:"Security"', 'label:"\u5b89\u5168"'),
    ('label:"\u81ea\u5b9a\u4e49"', 'label:"\u5b9a\u5236"'),
    ('label:"Customize"', 'label:"\u5b9a\u5236"'),
    ('"Custom"', '"\u81ea\u5b9a\u4e49"'),
    ('label:"\u72b6\u6001"', 'label:"\u72b6\u6001"'),
    ('label:"Status"', 'label:"\u72b6\u6001"'),
    ('label:"\u73af\u5883"', 'label:"\u73af\u5883"'),
    ('label:"Environment"', 'label:"\u73af\u5883"'),
    ('chat:"\u65b0\u5efa\u804a\u5929"', 'chat:"\u65b0\u5efa\u804a\u5929"'),
    ('chat:"New chat"', 'chat:"\u65b0\u5efa\u804a\u5929"'),
    ('cowork:"\u65b0\u5efa\u4efb\u52a1"', 'cowork:"\u65b0\u5efa\u4efb\u52a1"'),
    ('cowork:"New task"', 'cowork:"\u65b0\u5efa\u4efb\u52a1"'),
    ('code:"\u65b0\u5efa\u4f1a\u8bdd"', 'code:"\u65b0\u5efa\u4f1a\u8bdd"'),
    ('code:"New session"', 'code:"\u65b0\u5efa\u4f1a\u8bdd"'),
    ('operon:"\u65b0\u5efa\u4f1a\u8bdd"', 'operon:"\u65b0\u5efa\u4f1a\u8bdd"'),
    ('operon:"New session"', 'operon:"\u65b0\u5efa\u4f1a\u8bdd"'),
    ('oo="\u672c\u5730"', 'oo="\u672c\u5730"'),
    ('oo="Local"', 'oo="\u672c\u5730"'),
    ('io="\u4e91\u7aef"', 'io="\u4e91\u7aef"'),
    ('io="Cloud"', 'io="\u4e91\u7aef"'),
    ('ro="\u8fdc\u7a0b\u63a7\u5236"', 'ro="\u8fdc\u7a0b\u63a7\u5236"'),
    ('ro="Remote Control"', 'ro="\u8fdc\u7a0b\u63a7\u5236"'),
    ('co="\u5168\u90e8"', 'co="\u5168\u90e8"'),
    ('co="All"', 'co="\u5168\u90e8"'),
    ('const Ea="\u5df2\u5b89\u6392"', 'const Ea="\u5df2\u5b89\u6392"'),
    ('const Ea="Scheduled"', 'const Ea="\u5b9a\u65f6"'),
    ('["active","\u6d3b\u8dc3"]', '["active","\u6d3b\u8dc3"]'),
    ('["active","Active"]', '["active","\u6d3b\u8dc3"]'),
    ('["archived","\u5df2\u5f52\u6863"]', '["archived","\u5df2\u5f52\u6863"]'),
    ('["archived","Archived"]', '["archived","\u5df2\u5f52\u6863"]'),
    ('["all","\u5168\u90e8"]', '["all","\u5168\u90e8"]'),
    ('["all","All"]', '["all","\u5168\u90e8"]'),
    ('["0","\u5168\u90e8"]', '["0","\u5168\u90e8"]'),
    ('["0","All"]', '["0","\u5168\u90e8"]'),
    ('["1","\u0031\u5929"]', '["1","1\u5929"]'),
    ('["1","1d"]', '["1","1\u5929"]'),
    ('["3","\u0033\u5929"]', '["3","3\u5929"]'),
    ('["3","3d"]', '["3","3\u5929"]'),
    ('["7","\u0037\u5929"]', '["7","7\u5929"]'),
    ('["7","7d"]', '["7","7\u5929"]'),
    ('["14","14\u5929"]', '["14","14\u5929"]'),
    ('["14","14d"]', '["14","14\u5929"]'),
    ('["30","\u0033\u0030\u5929"]', '["30","30\u5929"]'),
    ('["30","30d"]', '["30","30\u5929"]'),
    ('"\u65e5\u671f"', '"\u65e5\u671f"'),
    ('"Date"', '"\u65e5\u671f"'),
    ('"\u65e0"', '"\u65e0"'),
    ('"None"', '"\u65e0"'),
    ('["project","\u9879\u76ee"]', '["project","Project"]'),
    ('["state","\u72b6\u6001"]', '["state","\u72b6\u6001"]'),
    ('["state","State"]', '["state","\u72b6\u6001"]'),
    ('?"\u5168\u90e8":', '?"\u5168\u90e8":'),
    ('?"All":', '?"\u5168\u90e8":'),
    ('children:"\u5df2\u56fa\u5b9a"', 'children:"\u5df2\u56fa\u5b9a"'),
    ('children:"Pinned"', 'children:"\u5df2\u56fa\u5b9a"'),
    ('children:"\u62d6\u62fd\u56fa\u5b9a"', 'children:"\u62d6\u62fd\u56fa\u5b9a"'),
    ('children:"Drag to pin"', 'children:"\u62d6\u62fd\u56fa\u5b9a"'),
    ('"Drop here"', '"\u653e\u5728\u8fd9\u91cc"'),
    ('"Let go"', '"\u677e\u5f00"'),
    ('children:["\u67e5\u770b\u5168\u90e8"', 'children:["\u67e5\u770b\u5168\u90e8"'),
    ('children:["View all"', 'children:["\u67e5\u770b\u5168\u90e8"'),
    ('title:"\u5220\u9664\u8f83\u65e7\u7684\u4f1a\u8bdd\uff1f"', 'title:"\u5220\u9664\u8f83\u65e7\u7684\u4f1a\u8bdd\uff1f"'),
    ('title:"Delete older sessions?"', 'title:"\u5220\u9664\u8f83\u65e7\u7684\u4f1a\u8bdd\uff1f"'),
    ('children:"\u6e05\u9664\u7b5b\u9009"', 'children:"\u6e05\u9664\u7b5b\u9009"'),
    ('children:"Clear filters"', 'children:"\u6e05\u9664\u7b5b\u9009"'),
    ('children:"\u6240\u6709\u9879\u76ee"', 'children:"\u6240\u6709\u9879\u76ee"'),
    ('children:"All projects"', 'children:"\u6240\u6709\u9879\u76ee"'),
    ('children:"\u5f00\u53d1\u9762\u677f"', 'children:"\u5f00\u53d1\u9762\u677f"'),
    ('children:"Dev panels"', 'children:"\u5f00\u53d1\u9762\u677f"'),
    ('children:"\u4e3b\u9898"', 'children:"\u4e3b\u9898"'),
    ('children:"Theme"', 'children:"\u4e3b\u9898"'),
    ('children:"\u5b57\u4f53"', 'children:"\u5b57\u4f53"'),
    ('children:"Font"', 'children:"\u5b57\u4f53"'),
    ('children:"\u9879\u76ee"', 'children:"\u9879\u76ee"'),
    ('children:"Project"', 'children:"\u9879\u76ee"'),
    ('const Co="\u6700\u8fd1"', 'const Co="\u6700\u8fd1"'),
    ('const Co="Recents"', 'const Co="\u6700\u8fd1"'),
    ('label:"\u6700\u8fd1\u6d3b\u52a8"', 'label:"\u6700\u8fd1\u6d3b\u52a8"'),
    ('label:"Last activity"', 'label:"\u6700\u8fd1\u6d3b\u52a8"'),
    ('label:"\u5206\u7ec4\u65b9\u5f0f"', 'label:"\u5206\u7ec4\u65b9\u5f0f"'),
    ('label:"Group by"', 'label:"\u5206\u7ec4\u65b9\u5f0f"'),
    ('"Stale after"', '"\u8fc7\u671f\u65f6\u95f4"'),
    ('"Older"', '"\u66f4\u65e9"'),
    ('"Ungrouped"', '"\u672a\u5206\u7ec4"'),
]

# === Index bundle (index-BlXy9TJN.js) - use glob pattern ===
PATCHES["index-*.js"] = [
    ('title:"\u8ba1\u5212\u4efb\u52a1"', 'title:"\u8ba1\u5212\u4efb\u52a1"'),
    ('title:"Scheduled tasks",subheader', 'title:"\u8ba1\u5212\u4efb\u52a1",subheader'),
    ('message:"\u8ba1\u5212\u4efb\u52a1\u4ec5\u5728\u8ba1\u7b97\u673a\u4fdd\u6301\u5524\u9192\u65f6\u8fd0\u884c\u3002"', 'message:"\u8ba1\u5212\u4efb\u52a1\u4ec5\u5728\u8ba1\u7b97\u673a\u4fdd\u6301\u5524\u9192\u65f6\u8fd0\u884c\u3002"'),
    ('message:"Scheduled tasks only run while your computer is awake."', 'message:"\u8ba1\u5212\u4efb\u52a1\u4ec5\u5728\u8ba1\u7b97\u673a\u4fdd\u6301\u5524\u9192\u65f6\u8fd0\u884c\u3002"'),
    ('Keep awake', '\u4fdd\u6301\u5524\u9192'),
    ('Ifn={all:"\u5168\u90e8",active:"\u6d3b\u8dc3",archived:"\u5df2\u5f52\u6863"}', 'Ifn={all:"\u5168\u90e8",active:"\u6d3b\u8dc3",archived:"\u5df2\u5f52\u6863"}'),
    ('Ifn={all:"All",active:"Active",archived:"Archived"}', 'Ifn={all:"\u5168\u90e8",active:"\u6d3b\u8dc3",archived:"\u5df2\u5f52\u6863"}'),
    ('"No tasks yet."', '"\u8fd8\u6ca1\u6709\u4efb\u52a1\u3002"'),
    ('"No active tasks."', '"\u6ca1\u6709\u6d3b\u8dc3\u4efb\u52a1\u3002"'),
    ('"No archived tasks."', '"\u6ca1\u6709\u5df2\u5f52\u6863\u4efb\u52a1\u3002"'),
    ('children:"\u6d3b\u8dc3"}),renderRow', 'children:"\u6d3b\u8dc3"}),renderRow'),
    ('children:"Active"}),renderRow', 'children:"\u6d3b\u8dc3"}),renderRow'),
    ('children:"\u65b0\u5efa\u4efb\u52a1"', 'children:"\u65b0\u5efa\u4efb\u52a1"'),
    ('children:"New task"', 'children:"\u65b0\u5efa\u4efb\u52a1"'),
    ('?"\u65b0\u5efa\u4efb\u52a1":"\u65b0\u5efa\u804a\u5929"', '?"\u65b0\u5efa\u4efb\u52a1":"\u65b0\u5efa\u804a\u5929"'),
    ('?"New task":"New chat"', '?"\u65b0\u5efa\u4efb\u52a1":"\u65b0\u5efa\u804a\u5929"'),
    ('baseDescription:"\u65b0\u5efa\u4efb\u52a1"', 'baseDescription:"\u65b0\u5efa\u4efb\u52a1"'),
    ('baseDescription:"New task"', 'baseDescription:"\u65b0\u5efa\u4efb\u52a1"'),
    ('title:"\u4efb\u52a1"', 'title:"\u4efb\u52a1"'),
    ('nextRun:"\u4e0b\u6b21\u8fd0\u884c",name:"\u540d\u79f0"', 'nextRun:"\u4e0b\u6b21\u8fd0\u884c",name:"\u540d\u79f0"'),
    ('nextRun:"Next run",name:"Name"', 'nextRun:"\u4e0b\u6b21\u8fd0\u884c",name:"\u540d\u79f0"'),
    ('children:"3P"', 'children:"\u7b2c\u4e09\u65b9"'),
    ('label:"\u6587\u6863"', 'label:"\u6587\u6863"'),
    ('label:"Documents"', 'label:"\u6587\u6863"'),
    ('label:"\u6587\u4ef6"', 'label:"\u6587\u4ef6"'),
    ('label:"Files"', 'label:"\u6587\u4ef6"'),
    ('label:"\u540c\u6b65\u6e90"', 'label:"\u540c\u6b65\u6e90"'),
    ('label:"Sync Sources"', 'label:"\u540c\u6b65\u6e90"'),
    ('title:"\u4ece GitHub \u6dfb\u52a0\u5185\u5bb9"', 'title:"\u4ece GitHub \u6dfb\u52a0\u5185\u5bb9"'),
    ('title:"Add content from GitHub"', 'title:"\u4ece GitHub \u6dfb\u52a0\u5185\u5bb9"'),
    ('title:"\u5c06 Claude \u8fde\u63a5\u5230 Google Drive"', 'title:"\u5c06 Claude \u8fde\u63a5\u5230 Google Drive"'),
    ('title:"Connect Claude to Google Drive"', 'title:"\u5c06 Claude \u8fde\u63a5\u5230 Google Drive"'),
    ('title:"\u7ed3\u675f\u6b64\u901a\u8bdd\uff1f"', 'title:"\u7ed3\u675f\u6b64\u901a\u8bdd\uff1f"'),
    ('title:"End this call?"', 'title:"\u7ed3\u675f\u6b64\u901a\u8bdd\uff1f"'),
    ('title:"\u4ee3\u7801\u6267\u884c\u4e0e\u6587\u4ef6\u521b\u5efa"', 'title:"\u4ee3\u7801\u6267\u884c\u4e0e\u6587\u4ef6\u521b\u5efa"'),
    ('title:"Code execution and file creation"', 'title:"\u4ee3\u7801\u6267\u884c\u4e0e\u6587\u4ef6\u521b\u5efa"'),
]


def collect_chunk_mutation_targets(app_resources: Path) -> list[Path]:
    """Return the closed file set that chunk patching may modify.

    Unknown hashed replacements are deliberately limited to index bundles by
    ``patch_assets_tree``; including every index file here therefore seals the
    cold-discovery fallback without scanning/copying unrelated asset chunks.
    """
    targets: set[Path] = set()
    for assets_dir in iter_assets_dirs(app_resources):
        targets.update(path for path in assets_dir.glob("index-*.js") if path.is_file())
        for pattern in PATCHES:
            files = [path for path in assets_dir.glob(pattern) if path.is_file()]
            if not files:
                prefix = stable_prefix_pattern(pattern)
                if prefix:
                    files = [path for path in assets_dir.glob(prefix) if path.is_file()]
            targets.update(files)
    return sorted(targets, key=lambda path: path.as_posix().lower())


def apply_chunks_with_cache(
    app_dir: Path,
    app_resources: Path,
    progress_cb=None,
    *,
    backup: bool = True,
) -> dict:
    """Apply chunks through cache-hit, legacy-upgrade, or full-discovery paths."""

    def report(percent: int, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(percent, message)
            except Exception:
                pass

    signature = patch_signature()
    fingerprint_before = assets_fingerprint(app_resources)
    normalized_app = normalize_app_dir(app_dir)
    state = load_chunk_state()
    if (
        state
        and state.get("app_dir") == normalized_app
        and state.get("patch_signature") == signature
        and state.get("asset_fingerprint") == fingerprint_before
    ):
        report(80, "JS chunk 未变化，已跳过重复扫描")
        return {
            "chunk_patches": 0,
            "cache_hit": True,
            "upgrade_mode": False,
            "target_files": list(state.get("target_files", [])),
        }

    upgrade_mode = has_complete_runtime_markers(app_resources)
    if upgrade_mode:
        report(20, "检测到旧版补丁，正在快速升级...")
    else:
        report(20, "首次适配当前 Claude 版本...")

    if backup:
        for path in collect_chunk_mutation_targets(app_resources):
            # Every target belongs to one of the discovered assets directories.
            assets_dir = path.parent
            if not backup_file(path, assets_dir):
                raise OSError(f"Failed to back up chunk mutation plan: {path}")

    targets: set[str] = set()
    total = patch_assets_tree(
        app_resources,
        progress_cb=progress_cb,
        mode="upgrade" if upgrade_mode else "full",
        target_sink=targets,
        backup=False,
    )
    fingerprint_after = assets_fingerprint(app_resources)
    save_chunk_state(
        {
            "schema": CHUNK_STATE_SCHEMA,
            "app_dir": normalized_app,
            "asset_fingerprint": fingerprint_after,
            "patch_signature": signature,
            "target_files": sorted(targets),
            "chunk_patches": total,
            "completed_at": int(time.time()),
        }
    )
    return {
        "chunk_patches": total,
        "cache_hit": False,
        "upgrade_mode": upgrade_mode,
        "target_files": sorted(targets),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch Claude Desktop JS chunks with zh-CN labels")
    parser.add_argument("--app-dir", type=str, default=None)
    args = parser.parse_args()

    if args.app_dir:
        app_dir = Path(args.app_dir)
    else:
        app_dir = find_claude_package()

    if not app_dir or not app_dir.exists():
        raise SystemExit("Claude app directory not found.")

    elevation_exit = ensure_admin_for_windowsapps(
        app_dir,
        Path(__file__).resolve(),
        sys.argv[1:],
    )
    if elevation_exit is not None:
        return elevation_exit

    assets_root = app_dir / "resources" / "ion-dist" / "assets"
    if not assets_root.exists():
        raise SystemExit(f"Assets root not found: {assets_root}")

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    patch_result = apply_chunks_with_cache(app_dir, app_dir / "resources")
    total = patch_result["chunk_patches"]

    print(f"Done. Total chunk patches: {total}")
    print(f"Chunk cache hit: {patch_result['cache_hit']}")
    print(f"Legacy upgrade mode: {patch_result['upgrade_mode']}")
    return 0


def run_patch_chunks(app_dir, progress_cb=None, *, backup: bool = True) -> dict:
    """Programmatic entry: chunk text replacements + entry fingerprint marker.

    Unlike main(), returns a dict and does NOT self-elevate. progress_cb is an
    optional callable(percent, message).
    """
    def report(percent: int, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(percent, message)
            except Exception:
                pass

    t0 = time.time()
    app_dir = Path(app_dir)
    app_resources = app_dir / "resources"
    assets_root = app_resources / "ion-dist" / "assets"
    if not assets_root.exists():
        return {
            "success": False,
            "error": f"Assets root not found: {assets_root}",
            "chunk_patches": 0,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    report(10, "写入 JS chunk 文案替换与补丁指纹...")
    try:
        if backup:
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        patch_result = apply_chunks_with_cache(
            app_dir,
            app_resources,
            progress_cb=report,
            backup=backup,
        )
    except OSError as exc:
        return {
            "success": False,
            "error": str(exc),
            "chunk_patches": 0,
            "duration_ms": int((time.time() - t0) * 1000),
        }
    total = patch_result["chunk_patches"]
    report(100, "chunk 补丁完成")

    return {
        "success": True,
        "error": None,
        "chunk_patches": total,
        "cache_hit": patch_result["cache_hit"],
        "upgrade_mode": patch_result["upgrade_mode"],
        "target_files": patch_result["target_files"],
        "duration_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    raise SystemExit(main())
