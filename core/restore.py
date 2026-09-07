#!/usr/bin/env python3
"""Strict restore entry point.

Production restore is delegated to :mod:`installer` and therefore requires a
matching, complete, hash-verified manifest. Legacy flat backups are never
selected automatically and official bundle text is never scrubbed heuristically.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import patch_chunks as patch_chunks_zh_cn
from safe_io import atomic_copy, atomic_write_text


SKIP_RESTORE_NAMES = {
    "app.asar",
    "patch-state.json",
    patch_chunks_zh_cn.CHUNK_STATE_NAME,
}


def restore_from(backup_root: Path, target_root: Path) -> int:
    """Explicit fixture/legacy helper; never used by the production restore path."""
    restored = 0
    backup_root = Path(backup_root)
    target_root = Path(target_root)
    for source in backup_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(backup_root)
        if relative.name in SKIP_RESTORE_NAMES:
            continue
        destination = target_root / relative
        atomic_copy(source, destination)
        restored += 1
    return restored


def remove_chunk_state() -> bool:
    path = patch_chunks_zh_cn.chunk_state_path()
    try:
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed
    except OSError:
        return False


def revert_chunk_translations(app_resources: Path) -> int:
    """Fixture helper for replacement round-trip tests.

    It is deliberately not called by ``run_restore``. Real recovery restores
    exact snapshot bytes instead of guessing an inverse text replacement.
    """
    assets_root = Path(app_resources) / "ion-dist" / "assets"
    if not assets_root.is_dir():
        return 0
    reverse: dict[str, str] = {}
    for replacements in patch_chunks_zh_cn.PATCHES.values():
        for source, translated in replacements:
            if source != translated:
                reverse.setdefault(translated, source)
    pairs = list(reverse.items())
    changed = 0
    for path in sorted(assets_root.rglob("*.js"), key=lambda item: item.as_posix().lower()):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        content, count = patch_chunks_zh_cn.replace_outside_injected_blocks(content, pairs)
        if count:
            atomic_write_text(path, content)
            changed += 1
    return changed


def run_restore(app_dir, progress_cb=None, *, elevated: bool = False) -> dict:
    import installer

    return installer.run_restore(app_dir, progress_cb=progress_cb, elevated=elevated)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore Claude Desktop from a verified snapshot")
    parser.add_argument("--app-dir", required=True, help="Claude app directory")
    args = parser.parse_args()
    result = run_restore(Path(args.app_dir))
    print(result.get("message", "恢复失败"))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
