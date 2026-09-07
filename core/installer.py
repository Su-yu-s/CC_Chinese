"""Safe orchestration for patching, restoring, and opening Claude Desktop."""

from __future__ import annotations

import copy
import json
import stat
import tempfile
from pathlib import Path

import backup_manifest as bm
import compatibility as compat
import detector
import diagnostics as diag
import patch_chunks
import patch_json
from best_effort_io import (
    PermissionTransaction,
    SecurityDescriptorRecord,
    is_windows_admin,
    is_windowsapps_path,
)
from safe_io import atomic_copy, atomic_write_bytes, sha256_file


TOOL_VERSION = "1.0.0"
SIDEBAR_MESSAGES = {
    "bW7B87wFFp": "新建",
    "UxTJRaKagI": "项目",
    "eW5eoWkxy3": "作品",
    "cXAlMRerxW": "定时",
    "TXpOBiuxud": "定制",
}


def _report(callback, percent: int, message: str) -> None:
    if callback:
        try:
            callback(percent, message)
        except Exception:
            pass


def _failure(
    code: str,
    message: str,
    *,
    state: str = "error",
    cause=None,
    action: str = "install",
    diagnostic_context: dict[str, str | int] | None = None,
) -> dict:
    result = {"success": False, "state": state, "error_code": code, "message": message}
    if cause is not None:
        result["exception"] = cause
    if diagnostic_context:
        result["diagnostic_context"] = dict(diagnostic_context)
    try:
        diag.DiagnosticLogger().record_failure(action=action, result=result, context=diagnostic_context)
    except Exception:
        pass
    return result


def _target_plan(resources: Path, *, include_statsig: bool = True) -> list[Path]:
    targets = set(patch_json.collect_json_mutation_targets(resources, include_statsig=include_statsig))
    targets.update(patch_chunks.collect_chunk_mutation_targets(resources))
    return sorted(targets, key=lambda path: path.as_posix().lower())


def _snapshot_location(app_dir: Path, protected: bool) -> tuple[dict, Path, Path]:
    identity = bm.build_install_identity(app_dir)
    root = bm.select_snapshots_root(protected=protected)
    return identity, root, root / identity["install_id"]


def _load_existing_snapshot(app_dir: Path, protected: bool):
    identity, root, snapshot = _snapshot_location(app_dir, protected)
    if not (snapshot / bm.MANIFEST_FILE_NAME).is_file():
        if snapshot.exists():
            # A directory without a manifest is a crash remnant (e.g. the process
            # died between directory creation and the first manifest write).
            # It can never be loaded or restored, so discard it and start
            # fresh instead of blocking every future install.
            if not bm.discard_incomplete_snapshot(snapshot, expected_root=root):
                raise bm.ManifestStateError("快照目录不完整，需要先处理恢复状态。")
        return identity, root, snapshot, None
    try:
        loaded = bm.load_matching_manifest(root, identity)
        return identity, root, snapshot, loaded.manifest
    except bm.ManifestStateError:
        # A sealed-but-incomplete snapshot can only be produced after the
        # original baseline was captured. Retain it for a dedicated recovery
        # pass instead of discarding the only trustworthy rollback source.
        manifest = bm.load_manifest(snapshot)
        bm.validate_manifest(app_dir, manifest, snapshot, require_complete=False)
        if bm.manifest_phase(manifest) == bm.PHASE_SEALED:
            return identity, root, snapshot, manifest
        raise


def _ensure_protected_root() -> dict:
    # Local import avoids a module cycle: elevation's legacy CLI entry imports
    # installer only after the elevation module has finished loading.
    import elevation

    return elevation.ensure_protected_snapshot_root()


def _config_path() -> Path:
    return detector.resolve_claude_config()


def _verify_localized(app_dir: Path, *, include_statsig: bool = True) -> tuple[bool, str]:
    resources = app_dir / "resources"
    destinations = [
        resources / "zh-CN.json",
        resources / "ion-dist" / "i18n" / "zh-CN.json",
    ]
    if include_statsig:
        destinations.append(resources / "ion-dist" / "i18n" / "statsig" / "zh-CN.json")
    if not all(path.is_file() for path in destinations):
        return False, "中文资源文件未完整写入"
    try:
        messages = json.loads(destinations[1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False, "前端中文资源无法读取"
    if any(messages.get(key) != value for key, value in SIDEBAR_MESSAGES.items()):
        return False, "五项侧边栏翻译校验失败"
    if not patch_chunks.has_complete_runtime_markers(resources):
        return False, "JS 运行时补丁标记不完整"
    try:
        config = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False, "Claude locale 配置无法读取"
    if not isinstance(config, dict) or config.get("locale") != "zh-CN":
        return False, "Claude locale 尚未设置为 zh-CN"
    profile = compat.evaluate_profiles(resources)
    if profile.get("status") != compat.STATUS_VERIFIED:
        return False, "补丁后的资源布局未通过兼容性复检"
    return True, "ok"


def _restore_snapshot_files(resources: Path, snapshot: Path, manifest: dict, progress_cb=None) -> tuple[bool, str]:
    """Restore every manifest file; one failure must not strand the rest.

    A single locked or missing file used to abort the loop, leaving every
    later file in its patched state and the install in an unrecoverable
    half-patched condition.  All entries are now attempted and the failures
    are aggregated for the caller.
    """
    failures: list[str] = []
    files = manifest.get("files", [])
    for index, record in enumerate(files, 1):
        _report(progress_cb, 45 + int(30 * index / max(len(files), 1)), f"恢复文件 {index}/{len(files)}...")
        rel = record["relative_path"]
        try:
            target = bm.resolve_resources_path(resources, rel)
            if record["existed"]:
                # Recovery commonly reaches this point after the prior run
                # already restored most files. Avoid an unnecessary protected
                # atomic replace when the official byte hash already matches.
                if target.is_file() and sha256_file(target) == record.get("original_sha256"):
                    continue
                atomic_copy(bm.payload_path(snapshot, rel), target)
            else:
                if target.exists():
                    target.unlink()
        except OSError as exc:
            failures.append(f"{rel}: {exc}")
    if not bm.restore_user_config(manifest, _config_path()):
        failures.append("用户 locale/font 原值恢复失败")
    if failures:
        return False, "; ".join(failures)
    return True, "ok"


def _verify_restored(resources: Path, manifest: dict) -> tuple[bool, str]:
    for record in manifest.get("files", []):
        rel = record["relative_path"]
        target = bm.resolve_resources_path(resources, rel)
        if record["existed"]:
            if not target.is_file() or sha256_file(target) != record.get("original_sha256"):
                return False, f"官方文件哈希未恢复：{rel}"
        elif target.exists():
            return False, f"补丁创建文件仍存在：{rel}"
    config_record = manifest.get("user_config", {})
    if config_record:
        path = _config_path()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                return False, "恢复后的用户配置无法读取"
            if not isinstance(data, dict):
                return False, "恢复后的用户配置不是 JSON 对象"
        else:
            data = {}
        for manifest_key, config_key in (("locale", "locale"), ("font", "claudeZhCnFont")):
            original = config_record.get(manifest_key, {})
            if original.get("existed"):
                if config_key not in data or data.get(config_key) != original.get("value"):
                    return False, f"配置原值未恢复：{config_key}"
            elif config_key in data:
                return False, f"补丁配置仍存在：{config_key}"
    return True, "ok"


def _manifest_descriptor_map(manifest: dict) -> dict[str, SecurityDescriptorRecord]:
    records: dict[str, SecurityDescriptorRecord] = {}
    for value in manifest.get("security_descriptors", []):
        try:
            record = SecurityDescriptorRecord.from_dict(value)
            records[record.path] = record
        except (KeyError, TypeError, ValueError):
            continue
    return records


def _use_manifest_restore_descriptors(permissions: PermissionTransaction, manifest: dict) -> None:
    """Replace captured restore records with sealed originals where present."""
    originals = _manifest_descriptor_map(manifest)
    # Files created by the patch have no original ACL. Once restore removes
    # them, their absence is success, not a missing-official-file failure.
    for item in manifest.get("files", []):
        if not item["existed"]:
            rel = bm.normalize_resources_relative_path(item["relative_path"])
            originals[rel] = SecurityDescriptorRecord(path=rel, existed=False)
    permissions.records = [originals.get(record.path, record) for record in permissions.records]


def _descriptors_match_manifest(permissions: PermissionTransaction, manifest: dict) -> bool:
    current = {record.path: record for record in permissions.records}
    originals = _manifest_descriptor_map(manifest)
    if not originals:
        return True
    return all(current.get(path) == record for path, record in originals.items())


def _recover_incomplete_install(
    app_dir: Path,
    resources: Path,
    snapshot: Path,
    manifest: dict,
    *,
    protected: bool,
) -> tuple[bool, str]:
    """Return a sealed-but-incomplete install to its captured baseline.

    This is intentionally separate from a new install: it never continues
    into a fresh patch, so a prior failed run cannot compound changes.
    """
    targets = [bm.resolve_resources_path(resources, item["relative_path"]) for item in manifest["files"]]
    permissions = PermissionTransaction(app_dir, targets)
    transaction_ok = True
    try:
        if protected:
            permissions.capture()
            content_ok, _ = _verify_restored(resources, manifest)
            if content_ok and _descriptors_match_manifest(permissions, manifest):
                return True, "ok"
            # Restore each target exactly once. Manifest records are the
            # authority for paths touched by the failed transaction; newly
            # added atomic-write parent paths retain their freshly captured
            # descriptors.
            _use_manifest_restore_descriptors(permissions, manifest)
        if not permissions.prepare():
            return False, "无法取得清理未完成汉化所需的写权限。"
        restored, detail = _restore_snapshot_files(resources, snapshot, manifest)
        if not restored:
            return False, detail
        verified, detail = _verify_restored(resources, manifest)
        if not verified:
            return False, detail
    except (OSError, PermissionError, ValueError) as exc:
        return False, str(exc)
    finally:
        transaction_ok = permissions.restore()
    if not transaction_ok:
        return False, "文件内容已恢复，但原始权限未能完整恢复。"
    return True, "ok"


def _cleanup_tool_state() -> None:
    for path in (patch_json.PATCH_STATE_PATH, patch_chunks.chunk_state_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class _CurrentStateRollback:
    """Temporary copy of the pre-restore state, used only if restore fails."""

    def __init__(self, resources: Path, manifest: dict, config_path: Path) -> None:
        self.resources = resources
        self.manifest = manifest
        self.config_path = config_path
        # Own a concrete directory, without TemporaryDirectory's recursive
        # permission-repair finalizer running on the transaction thread.
        self.root = Path(tempfile.mkdtemp(prefix="cczh-restore-")).resolve()
        self.files: list[tuple[str, bool]] = []
        self.config_existed = config_path.is_file()

    def capture(self) -> None:
        for record in self.manifest.get("files", []):
            rel = record["relative_path"]
            source = bm.resolve_resources_path(self.resources, rel)
            existed = source.is_file()
            self.files.append((rel, existed))
            if existed:
                atomic_copy(source, self.root / "resources" / Path(rel))
        if self.config_existed:
            atomic_copy(self.config_path, self.root / "config.json")

    def restore(self, progress_cb=None) -> bool:
        ok = True
        for index, (rel, existed) in enumerate(self.files, 1):
            _report(progress_cb, 92, f"回退文件 {index}/{len(self.files)}：{rel}")
            try:
                target = bm.resolve_resources_path(self.resources, rel)
                if existed:
                    source = self.root / "resources" / Path(rel)
                    if not target.is_file() or sha256_file(target) != sha256_file(source):
                        atomic_copy(source, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                ok = False
        try:
            if self.config_existed:
                atomic_copy(self.root / "config.json", self.config_path)
            else:
                self.config_path.unlink(missing_ok=True)
        except OSError:
            ok = False
        return ok

    def close(self) -> bool:
        try:
            return self._close_files()
        except OSError:
            # Even directory inspection can be denied by a transient lock.
            # Keep the copies and let the caller report cleanup separately.
            return False

    def _close_files(self) -> bool:
        """One bounded pass over our recorded copies; never recurse/retry a tree.

        Temporary cleanup is not part of restoring official files. A locked
        copy may remain, but must not mask a completed restore or hang its UI.
        Only empty directories are removed; unexpected entries are retained.
        """
        if not self.root.exists():
            return True
        candidates = [self.root / "resources" / rel for rel, _ in self.files]
        candidates.append(self.root / "config.json")
        directories = {self.root}
        ok = True
        for path in candidates:
            # Check resolved bounds before any chmod/delete; do not follow a
            # replaced root or a junction into another directory.
            if self.root.is_symlink() or self.root.is_junction() or not path.resolve().is_relative_to(self.root):
                ok = False
                continue
            parent = path.parent
            while parent != self.root:
                directories.add(parent)
                parent = parent.parent
            try:
                path.unlink(missing_ok=True)
            except PermissionError:
                try:
                    path.chmod(stat.S_IREAD | stat.S_IWRITE)
                    path.unlink(missing_ok=True)
                except OSError:
                    ok = False
            except OSError:
                ok = False
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            try:
                if directory.is_symlink() or directory.is_junction() or not directory.resolve().is_relative_to(self.root):
                    ok = False
                    continue
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                ok = False
        return ok


def _restore_preflight(resources: Path, manifest: dict) -> tuple[bool, str]:
    for record in manifest.get("files", []):
        rel = record["relative_path"]
        target = bm.resolve_resources_path(resources, rel)
        if not target.is_file():
            if record["existed"]:
                return False, f"应存在的文件已缺失：{rel}"
            continue
        digest = sha256_file(target)
        allowed = {value for value in (record.get("original_sha256"), record.get("patched_sha256")) if value}
        if digest not in allowed:
            return False, f"文件已被其他版本修改：{rel}"
    return True, "ok"


def run_install(app_dir, progress_cb=None, backup=True, *, elevated: bool = False) -> dict:
    """Install the Chinese patch through a sealed snapshot transaction.

    ``backup`` is retained for source compatibility but cannot disable the
    mandatory safety snapshot.
    """
    del backup
    app_dir = Path(app_dir) if app_dir else Path()
    resources = app_dir / "resources"
    if not resources.is_dir():
        return _failure("LOCATE_NOT_FOUND", "未找到 Claude Desktop 安装目录。", state="missing")
    protected = is_windowsapps_path(app_dir)
    # The Store package's Statsig subdirectory is sometimes separately
    # immutable even to an elevated Administrator.  It is not required for
    # the visible UI locale, so never make the primary patch depend on it.
    include_statsig = not protected
    source_check = patch_json.validate_source_resources(include_statsig=include_statsig)
    if not source_check["success"]:
        return _failure(
            source_check["error_code"],
            source_check["error"],
            cause=source_check.get("exception"),
            diagnostic_context=source_check["diagnostic_context"],
        )
    if protected and (not elevated or not is_windows_admin()):
        return _failure("PERMISSION_DENIED", "WindowsApps 版本需要管理员授权后才能汉化。")

    _report(progress_cb, 8, "检查当前 Claude 版本兼容性...")
    profile = compat.evaluate_profiles(resources, version=detector.version_label(app_dir))
    if profile.get("status") != compat.STATUS_VERIFIED:
        code = "COMPAT_UNSUPPORTED" if profile.get("status") == compat.STATUS_UNSUPPORTED else "COMPAT_UNKNOWN"
        return _failure(code, "当前 Claude 资源结构尚未通过适配验证，未修改任何文件。", state="compat_error")

    if protected:
        root_result = _ensure_protected_root()
        if not root_result.get("success"):
            return _failure(
                str(root_result.get("error_code", "SNAPSHOT_ROOT_INIT_FAILED")),
                str(root_result.get("message", "安全快照目录初始化失败，未修改 Claude。")),
            )

    try:
        _, root, snapshot, existing = _load_existing_snapshot(app_dir, protected)
    except bm.ManifestMismatchError as exc:
        return _failure("SNAPSHOT_MISMATCH", "现有快照与当前 Claude 版本不匹配。", cause=exc)
    except bm.ManifestError as exc:
        return _failure("SNAPSHOT_INVALID", "现有安全快照不完整或已损坏。", cause=exc)

    if existing is not None and bm.manifest_phase(existing) != bm.PHASE_COMPLETE:
        _report(progress_cb, 12, "清理上次未完成的汉化状态...")
        recovered, detail = _recover_incomplete_install(
            app_dir, resources, snapshot, existing, protected=protected
        )
        if not recovered:
            return _failure("PATCH_RECOVERY_FAILED", f"无法安全清理上次未完成状态：{detail}")
        if not bm.discard_incomplete_snapshot(snapshot, expected_root=root):
            return _failure("PATCH_RECOVERY_FAILED", "已恢复文件，但未能安全清理未完成快照。")
        _cleanup_tool_state()
        return {
            "success": True,
            "state": "repair",
            "message": "已安全清理上次未完成的汉化状态，请再次点击汉化。",
        }

    baseline = compat.evaluate_baseline(resources, profile, user_config=_config_path(), manifest=existing)
    if baseline["status"] == compat.BASELINE_EXPECTED_PATCHED:
        verified, detail = _verify_localized(app_dir, include_statsig=include_statsig)
        if verified:
            return {"success": True, "state": "ready", "message": "当前版本已经完成汉化，无需重复处理。"}
        return _failure("PATCH_VERIFY_FAILED", detail)
    allowed = {compat.BASELINE_CLEAN, compat.BASELINE_RESTORED_WITH_MANIFEST}
    if baseline["status"] not in allowed:
        return _failure("BASELINE_DIRTY", "检测到不完整或来源不明的修改，未继续汉化。", state="baseline_dirty")

    targets = _target_plan(resources, include_statsig=include_statsig)
    if not targets:
        return _failure("COMPAT_UNKNOWN", "当前版本没有可验证的补丁目标。")
    _report(progress_cb, 16, "正在关闭 Claude Desktop...")
    if not detector.stop_claude():
        # 运行中的 Claude 会锁定资源文件，写入阶段必然失败；再确认一次。
        _report(progress_cb, 17, "等待 Claude 完全退出...")
        detector.stop_claude()
    permissions = PermissionTransaction(app_dir, targets)
    try:
        descriptors = permissions.capture() if protected else []
    except (OSError, PermissionError, ValueError) as exc:
        return _failure("PERMISSION_DENIED", "无法读取目标文件权限，未执行修改。", cause=exc)

    created_snapshot = False
    manifest = existing
    try:
        if manifest is None:
            _report(progress_cb, 25, "封存完整官方基线...")
            manifest, snapshot, created_snapshot = bm.create_sealed_snapshot(
                app_dir,
                targets,
                config_path=_config_path(),
                security_descriptors=descriptors,
                operations=("json", "chunks", "locale"),
                privileged=protected,
                profile_id=profile["profile_id"],
                patch_version=TOOL_VERSION,
            )
        else:
            requested = sorted(bm.resource_relative(path, resources) for path in targets)
            recorded = sorted(item["relative_path"] for item in manifest["files"])
            if requested != recorded:
                raise bm.ManifestMismatchError("快照记录的修改计划与当前工具不一致")
    except bm.ManifestMismatchError as exc:
        return _failure("SNAPSHOT_MISMATCH", "安全快照与当前补丁计划不匹配。", cause=exc)
    except bm.ManifestError as exc:
        if snapshot.exists():
            bm.discard_incomplete_snapshot(snapshot, expected_root=root)
        return _failure("SNAPSHOT_CREATE_FAILED", "安全基线创建失败，未修改 Claude。", cause=exc)

    if not permissions.prepare():
        if created_snapshot:
            bm.discard_incomplete_snapshot(snapshot, expected_root=root)
        return _failure(
            "PERMISSION_DENIED",
            "无法安全获取目标文件写权限。",
            diagnostic_context=permissions.diagnostic_context(),
        )

    working = manifest
    if bm.manifest_phase(manifest) == bm.PHASE_COMPLETE:
        working = copy.deepcopy(manifest)
        working["status"] = bm.STATUS_STAGING
        working["state"] = bm.STATUS_STAGING

    patch_error = None
    patch_context = {}
    patch_step = "json"
    try:
        _report(progress_cb, 42, "写入中文资源...")
        json_result = patch_json.run_patch(app_dir, backup=False, include_statsig=include_statsig)
        if not json_result.get("success"):
            patch_context = json_result.get("diagnostic_context", {})
            raise json_result.get("exception") or OSError(json_result.get("error") or "JSON 补丁失败")
        patch_step = "chunks"
        _report(progress_cb, 68, "写入界面翻译与运行时补丁...")
        chunk_result = patch_chunks.run_patch_chunks(app_dir, backup=False)
        if not chunk_result.get("success"):
            raise OSError(chunk_result.get("error") or "chunk 补丁失败")
        patch_step = "verify"
        _report(progress_cb, 88, "复检实际汉化结果...")
        verified, detail = _verify_localized(app_dir, include_statsig=include_statsig)
        if not verified:
            raise RuntimeError(detail)
        patch_step = "manifest"
        for record in working["files"]:
            bm.record_patched_file(working, resources, record["relative_path"])
        bm.commit_manifest(snapshot, working)
        patch_step = "restore_acl"
        if not permissions.restore():
            raise PermissionError("目标文件权限未能恢复")
    except Exception as exc:
        patch_error = exc

    if patch_error is not None:
        if protected:
            permissions.prepare()
        rollback_ok, _ = _restore_snapshot_files(resources, snapshot, manifest)
        acl_ok = permissions.restore()
        if rollback_ok and created_snapshot:
            bm.discard_incomplete_snapshot(snapshot, expected_root=root)
        _cleanup_tool_state()
        rollback_context = {
            **patch_context,
            "patch_step": patch_step,
            "rollback_content": int(rollback_ok),
            "rollback_acl": int(acl_ok),
        }
        if not rollback_ok or not acl_ok:
            return _failure(
                "PATCH_ROLLBACK_FAILED",
                "汉化失败且自动回滚未完整完成。",
                cause=patch_error,
                diagnostic_context=rollback_context,
            )
        code = "PATCH_VERIFY_FAILED" if isinstance(patch_error, RuntimeError) else "PATCH_WRITE_FAILED"
        return _failure(
            code,
            f"汉化未完成，已恢复修改前状态：{patch_error}",
            cause=patch_error,
            diagnostic_context=rollback_context,
        )

    _report(progress_cb, 100, "汉化完成并已通过复检。")
    return {
        "success": True,
        "state": "ready",
        "message": "汉化完成",
        "profile_id": profile["profile_id"],
        "snapshot_id": manifest["install_id"],
        "statsig_skipped": not include_statsig,
    }


def run_restore(app_dir, progress_cb=None, *, elevated: bool = False) -> dict:
    """Restore only from a matching, complete, hash-verified snapshot."""
    app_dir = Path(app_dir) if app_dir else Path()
    resources = app_dir / "resources"
    if not resources.is_dir():
        return _failure("LOCATE_NOT_FOUND", "未找到 Claude Desktop 安装目录。", state="missing", action="restore")
    protected = is_windowsapps_path(app_dir)
    if protected and (not elevated or not is_windows_admin()):
        return _failure("PERMISSION_DENIED", "WindowsApps 版本需要管理员授权后才能恢复。", action="restore")

    if protected:
        root_result = _ensure_protected_root()
        if not root_result.get("success"):
            return _failure(
                str(root_result.get("error_code", "SNAPSHOT_ROOT_INIT_FAILED")),
                str(root_result.get("message", "安全快照目录初始化失败，未修改 Claude。")),
                action="restore",
            )

    _report(progress_cb, 10, "校验匹配的完整安全快照...")
    try:
        identity, root, _ = _snapshot_location(app_dir, protected)
        loaded = bm.load_matching_manifest(root, identity)
    except bm.ManifestMismatchError as exc:
        return _failure("RESTORE_NO_MATCH", "没有与当前 Claude 版本严格匹配的完整快照。", cause=exc, action="restore")
    except bm.ManifestError as exc:
        return _failure("RESTORE_PREFLIGHT_FAILED", "安全快照不完整或校验失败。", cause=exc, action="restore")
    manifest = loaded.manifest
    snapshot = loaded.snapshot_path

    _report(progress_cb, 15, "正在关闭 Claude，等待文件释放...")
    if not detector.stop_claude():
        return _failure("RESTORE_APP_RUNNING", "Claude 未能完全退出，未开始恢复。", action="restore")
    _report(progress_cb, 20, "校验当前文件并定位需要恢复的内容...")
    preflight_ok, detail = _restore_preflight(resources, manifest)
    if not preflight_ok:
        return _failure("RESTORE_PREFLIGHT_FAILED", detail, action="restore")
    changed = []
    for item in manifest["files"]:
        target = bm.resolve_resources_path(resources, item["relative_path"])
        if item["existed"]:
            if sha256_file(target) != item["original_sha256"]:
                changed.append(item)
        elif target.exists():
            changed.append(item)
    # Full preflight/final verification still covers the whole baseline.
    # Only changed files need write access and a pre-restore rollback copy.
    restore_plan = {**manifest, "files": changed}
    targets = [bm.resolve_resources_path(resources, item["relative_path"]) for item in changed]
    rollback = _CurrentStateRollback(resources, restore_plan, _config_path())
    permissions = PermissionTransaction(app_dir, targets)
    permissions.progress_cb = lambda n, total: _report(progress_cb, 25 + int(10*n/max(total, 1)), f"读取恢复权限 {n}/{total}...")
    try:
        rollback.capture()
        if protected:
            permissions.capture()
            _use_manifest_restore_descriptors(permissions, manifest)
    except (OSError, PermissionError, ValueError) as exc:
        rollback.close()
        return _failure("RESTORE_PREFLIGHT_FAILED", "无法封存恢复前状态，未执行恢复。", cause=exc, action="restore")
    permissions.progress_cb = lambda n, total: _report(progress_cb, 35 + int(10*n/max(total, 1)), f"准备恢复权限 {n}/{total}...")
    if targets and not permissions.prepare():
        rollback.close()
        return _failure(
            "PERMISSION_DENIED",
            "无法安全获取恢复目标的写权限。",
            action="restore",
            diagnostic_context=permissions.diagnostic_context(),
        )

    restore_error = None
    try:
        _report(progress_cb, 45, "恢复官方文件和原始配置...")
        ok, detail = _restore_snapshot_files(resources, snapshot, restore_plan, progress_cb=progress_cb)
        if not ok:
            raise OSError(detail)
        _report(progress_cb, 78, "复检全部官方文件和原始配置...")
        verified, detail = _verify_restored(resources, manifest)
        if not verified:
            raise RuntimeError(detail)
        _cleanup_tool_state()
        permissions.progress_cb = lambda n, total: _report(progress_cb, 85 + int(14*n/max(total, 1)), f"还原官方权限 {n}/{total}...")
        if not permissions.restore():
            raise PermissionError("目标文件权限未能恢复")
    except Exception as exc:
        restore_error = exc

    if restore_error is not None:
        _report(progress_cb, 90, "恢复校验未通过，正在准备回退权限...")
        permissions.progress_cb = lambda n, total: _report(progress_cb, 90, f"准备回退权限 {n}/{total}...")
        if protected:
            if not permissions.prepare():
                return _failure(
                    "RESTORE_ROLLBACK_FAILED", "未取得回退写权限，已停止文件写入并保留临时回退副本。",
                    cause=restore_error, action="restore", diagnostic_context=permissions.diagnostic_context(),
                )
        _report(progress_cb, 92, "正在回退到恢复前的文件状态...")
        rollback_ok = rollback.restore(progress_cb=progress_cb)
        permissions.progress_cb = lambda n, total: _report(progress_cb, 95, f"归还回退权限 {n}/{total}...")
        acl_ok = permissions.restore()
        if not rollback_ok or not acl_ok:
            return _failure("RESTORE_ROLLBACK_FAILED", "恢复失败且事务回退未完整完成。", cause=restore_error, action="restore")
        _report(progress_cb, 99, "回退完成，清理本次临时文件...")
        rollback.close()
        return _failure("RESTORE_VERIFY_FAILED", f"恢复未完成，已回到操作前状态：{restore_error}", cause=restore_error, action="restore")

    _report(progress_cb, 99, "官方文件和权限已恢复，清理本次临时文件...")
    cleanup_ok = rollback.close()
    _report(progress_cb, 100, "已严格恢复官方原样。")
    return {
        "success": True,
        "state": "repair",
        "message": "恢复完成" if cleanup_ok else "恢复完成；部分临时回退文件被占用，未清理，不影响 Claude。",
        "temporary_cleanup_complete": cleanup_ok,
        "restored_files": len(manifest.get("files", [])),
        "snapshot_id": manifest["install_id"],
    }


def run_open(app_dir, progress_cb=None) -> dict:
    _report(progress_cb, 40, "正在打开 Claude Desktop...")
    ok, detail = detector.open_claude(app_dir)
    _report(progress_cb, 100, detail)
    return {
        "success": ok,
        "state": "ready" if ok else "error",
        "message": "Claude Desktop 已启动。" if ok else detail,
        "log": detail,
    }
