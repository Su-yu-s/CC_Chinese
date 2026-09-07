"""Version-bound, manifest-backed snapshots for safe patch and restore."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from best_effort_io import is_windowsapps_path
from safe_io import atomic_copy, atomic_write_text, sha256_file


SCHEMA_VERSION = 2
TOOL_VERSION = "1.0.0"
BACKUP_DIRECTORY_NAME = "Claude-zh-CN-official-backup"
MANIFEST_FILE_NAME = "manifest.json"
STATUS_STAGING = "staging"
STATUS_COMPLETE = "complete"
STATUS_INVALID = "invalid"
PHASE_STAGING = "staging"
PHASE_SEALED = "sealed"
PHASE_COMPLETE = "complete"
PHASE_INVALID = "invalid"


class ManifestError(RuntimeError):
    code = "SNAPSHOT_INVALID"


class ManifestFormatError(ManifestError):
    pass


class ManifestIntegrityError(ManifestError):
    pass


class ManifestStateError(ManifestError):
    pass


class ManifestMismatchError(ManifestError):
    code = "SNAPSHOT_MISMATCH"


SnapshotError = ManifestError
SnapshotCreateError = ManifestIntegrityError
SnapshotMismatch = ManifestMismatchError


@dataclass(frozen=True)
class OriginalValue:
    existed: bool
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"existed": self.existed, "value": self.value}


@dataclass(frozen=True)
class Verification:
    valid: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedSnapshot:
    snapshot_path: Path
    manifest: dict[str, Any]
    verification: Verification


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_resources_relative_path(value: str | os.PathLike[str]) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError("resource path must be path-like")
    raw = os.fspath(value).replace("\\", "/")
    if not raw or raw in {".", ".."} or raw.startswith("//"):
        raise ValueError(f"unsafe resources path: {value}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe resources path: {value}")
    if not path.parts or ":" in path.parts[0]:
        raise ValueError(f"unsafe resources path: {value}")
    return path.as_posix()


safe_resource_path = normalize_resources_relative_path


def resolve_resources_path(resources: Path, relative: str | os.PathLike[str]) -> Path:
    resources = Path(resources).resolve(strict=False)
    rel = normalize_resources_relative_path(relative)
    target = resources / Path(rel)
    try:
        target.resolve(strict=False).relative_to(resources)
    except ValueError as exc:
        raise ValueError(f"resource path escapes root: {relative}") from exc
    return target


def resource_relative(path: Path, resources: Path) -> str:
    resources = Path(resources).resolve(strict=False)
    try:
        rel = Path(path).resolve(strict=False).relative_to(resources)
    except ValueError as exc:
        raise ValueError(f"target escapes resources: {path}") from exc
    return normalize_resources_relative_path(PurePosixPath(*rel.parts))


def _infer_version(app_dir: Path) -> str:
    for name in (app_dir.name, app_dir.parent.name):
        match = re.search(r"(?:Claude[_-])?(\d+(?:\.\d+){1,3})", name, re.IGNORECASE)
        if match:
            return match.group(1)
    return "unknown"


# In-process identity cache. Detection re-runs after every action and would
# otherwise re-hash the multi-hundred-MB app.asar each time. The key contains
# the stable file's mtime and size, so any real change invalidates the entry.
_IDENTITY_CACHE: dict[tuple, dict[str, Any]] = {}
_IDENTITY_CACHE_LIMIT = 8


def _resource_layout(resources: Path) -> list[str]:
    """Return the JS layout without resolving every enumerated file.

    ``Path.rglob`` yields lexical descendants of ``assets`` and does not
    recurse through directory symlinks by default.  Calling ``resolve`` twice
    per file here made every status refresh perform thousands of expensive
    Windows ``GetFinalPathNameByHandle`` calls.  Mutation targets still go
    through ``resource_relative``/``resolve_resources_path``; this list is
    identity metadata only.
    """
    resources = Path(resources)
    assets = resources / "ion-dist" / "assets"
    if not assets.is_dir():
        return []
    return [
        normalize_resources_relative_path(path.relative_to(resources))
        for path in sorted(assets.rglob("*.js"), key=lambda item: item.as_posix().lower())
        if path.is_file()
    ]


def build_install_identity(app_dir: Path, *, version: str | None = None) -> dict[str, Any]:
    app_dir = Path(app_dir).resolve(strict=False)
    resources = app_dir / "resources"
    resource_layout = _resource_layout(resources)
    stable_file = resources / "app.asar"
    if not stable_file.is_file():
        executables = sorted(app_dir.glob("*.exe"), key=lambda item: item.name.lower())
        stable_file = executables[0] if executables else resources / "en-US.json"
    stable_stat = None
    try:
        stable_stat = stable_file.stat() if stable_file.is_file() else None
    except OSError:
        stable_stat = None
    cache_key = (
        os.path.normcase(str(app_dir)),
        version,
        stable_stat.st_mtime_ns if stable_stat else None,
        stable_stat.st_size if stable_stat else None,
        tuple(resource_layout),
    )
    cached = _IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    identity: dict[str, Any] = {
        "app_dir": os.path.normcase(str(app_dir)),
        "package": app_dir.parent.name if is_windowsapps_path(app_dir) else app_dir.name,
        "version": version or _infer_version(app_dir),
        "stable_file": stable_file.name,
        "stable_sha256": sha256_file(stable_file) if stable_stat is not None else "missing",
        "resource_layout": resource_layout,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    identity["install_id"] = hashlib.sha256(encoded).hexdigest()[:32]
    if len(_IDENTITY_CACHE) >= _IDENTITY_CACHE_LIMIT:
        _IDENTITY_CACHE.clear()
    _IDENTITY_CACHE[cache_key] = identity
    return identity


installation_identity = build_install_identity


def install_id(app_dir: Path) -> str:
    return str(build_install_identity(app_dir)["install_id"])


def select_snapshots_root(*, protected: bool, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    base = Path(env.get("PROGRAMDATA", r"C:\ProgramData")) if protected else Path(
        env.get("LOCALAPPDATA", str(Path.home()))
    )
    return base / BACKUP_DIRECTORY_NAME / "snapshots"


def backup_base(*, privileged: bool = False) -> Path:
    return select_snapshots_root(protected=privileged).parent


def snapshot_root_for(app_dir: Path, *, privileged: bool | None = None) -> Path:
    if privileged is None:
        privileged = is_windowsapps_path(Path(app_dir))
    return select_snapshots_root(protected=bool(privileged))


def manifest_path(app_dir: Path, *, privileged: bool | None = None) -> Path:
    identity = build_install_identity(app_dir)
    return snapshot_root_for(app_dir, privileged=privileged) / identity["install_id"] / MANIFEST_FILE_NAME


def payload_path(snapshot: Path, relative: str | os.PathLike[str]) -> Path:
    rel = normalize_resources_relative_path(relative)
    root = (Path(snapshot) / "payload" / "resources").resolve(strict=False)
    target = root / Path(rel)
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"payload path escapes snapshot: {relative}") from exc
    return target


_payload_path = payload_path


def _write_manifest(snapshot: Path, manifest: Mapping[str, Any]) -> None:
    snapshot = Path(snapshot)
    snapshot.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        snapshot / MANIFEST_FILE_NAME,
        json.dumps(dict(manifest), ensure_ascii=False, indent=2) + "\n",
    )


def create_manifest(
    identity: Mapping[str, Any], *, patch_version: str, profile_id: str | None = None
) -> dict[str, Any]:
    if not isinstance(identity.get("install_id"), str):
        raise ManifestFormatError("identity is missing install_id")
    return {
        "schema": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "patch_version": str(patch_version),
        "install_id": identity["install_id"],
        "identity": dict(identity),
        "profile_id": profile_id,
        "created_at": _utc_timestamp(),
        "sealed_at": None,
        "completed_at": None,
        "status": STATUS_STAGING,
        "state": STATUS_STAGING,
        "baseline_sealed": False,
        "invalid_reason": None,
        "files": [],
        "user_config": {},
        "security_descriptors": [],
        "operations": [],
    }


def manifest_phase(manifest: Mapping[str, Any]) -> str:
    status = manifest.get("status", manifest.get("state"))
    if status == STATUS_INVALID:
        return PHASE_INVALID
    if status == STATUS_COMPLETE:
        return PHASE_COMPLETE
    if manifest.get("baseline_sealed") is True:
        return PHASE_SEALED
    return PHASE_STAGING


def _validate_manifest_shape(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ManifestFormatError("manifest root is not an object")
    if manifest.get("schema") != SCHEMA_VERSION:
        raise ManifestFormatError("unsupported manifest schema")
    status = manifest.get("status", manifest.get("state"))
    if status not in {STATUS_STAGING, STATUS_COMPLETE, STATUS_INVALID}:
        raise ManifestFormatError("invalid manifest status")
    manifest["status"] = status
    manifest["state"] = status
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ManifestFormatError("manifest files is not a list")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ManifestFormatError("invalid manifest file entry")
        rel_value = entry.get("relative_path", entry.get("path", ""))
        try:
            rel = normalize_resources_relative_path(rel_value)
        except (TypeError, ValueError) as exc:
            raise ManifestFormatError(str(exc)) from exc
        if rel in seen:
            raise ManifestFormatError(f"duplicate manifest file: {rel}")
        seen.add(rel)
        entry["relative_path"] = rel
        entry["path"] = rel
        if not isinstance(entry.get("existed"), bool):
            raise ManifestFormatError(f"invalid existed flag: {rel}")
    return manifest


def load_manifest(snapshot: Path) -> dict[str, Any]:
    path = Path(snapshot)
    if path.is_dir() or path.suffix.lower() != ".json":
        path = path / MANIFEST_FILE_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestStateError(f"manifest not found: {path}") from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestFormatError(f"cannot read manifest: {exc}") from exc
    return _validate_manifest_shape(value)


def mark_invalid(manifest: dict[str, Any], reason: str, snapshot: Path | None = None) -> None:
    manifest["status"] = STATUS_INVALID
    manifest["state"] = STATUS_INVALID
    manifest["invalid_reason"] = str(reason)[:1000]
    if snapshot is not None:
        _write_manifest(snapshot, manifest)


def stage_resource_file(
    snapshot: Path,
    manifest: dict[str, Any],
    resources: Path,
    target: Path | str | os.PathLike[str],
) -> dict[str, Any]:
    if manifest_phase(manifest) != PHASE_STAGING:
        raise ManifestStateError("files can only be staged before baseline sealing")
    resources = Path(resources)
    candidate = Path(target)
    if candidate.is_absolute():
        rel = resource_relative(candidate, resources)
    else:
        rel = normalize_resources_relative_path(candidate)
    source = resolve_resources_path(resources, rel)
    if any(item.get("relative_path", item.get("path")) == rel for item in manifest["files"]):
        raise ManifestStateError(f"resource already staged: {rel}")
    existed = source.is_file()
    entry: dict[str, Any] = {
        "relative_path": rel,
        "path": rel,
        "existed": existed,
        "operation": "modify" if existed else "create",
        "original_sha256": None,
        "payload_sha256": None,
        "patched_sha256": None,
    }
    manifest["files"].append(entry)
    try:
        if existed:
            before = sha256_file(source)
            destination = payload_path(snapshot, rel)
            atomic_copy(source, destination)
            copied = sha256_file(destination)
            after = sha256_file(source)
            if before != copied or before != after:
                raise ManifestIntegrityError(f"resource changed while staging: {rel}")
            entry["original_sha256"] = before
            entry["payload_sha256"] = copied
        elif source.exists():
            raise ManifestIntegrityError(f"resource is not a regular file: {rel}")
        _write_manifest(snapshot, manifest)
        return entry
    except Exception as exc:
        mark_invalid(manifest, f"{rel}: {exc}", Path(snapshot))
        if isinstance(exc, ManifestError):
            raise
        raise ManifestIntegrityError(f"cannot stage {rel}: {exc}") from exc


def verify_payload(snapshot: Path, manifest: Mapping[str, Any]) -> Verification:
    reasons: list[str] = []
    try:
        value = _validate_manifest_shape(dict(manifest))
    except ManifestError as exc:
        return Verification(False, (str(exc),))
    for entry in value["files"]:
        if not entry["existed"]:
            continue
        rel = entry["relative_path"]
        expected = entry.get("payload_sha256")
        payload = payload_path(snapshot, rel)
        if not isinstance(expected, str) or not payload.is_file():
            reasons.append(f"payload missing: {rel}")
            continue
        try:
            if sha256_file(payload) != expected:
                reasons.append(f"payload hash mismatch: {rel}")
        except OSError as exc:
            reasons.append(f"payload unreadable: {rel}: {exc}")
    return Verification(not reasons, tuple(reasons))


def seal_baseline(snapshot: Path, manifest: dict[str, Any]) -> None:
    if manifest_phase(manifest) != PHASE_STAGING:
        raise ManifestStateError("baseline can only be sealed from staging")
    verification = verify_payload(snapshot, manifest)
    if not verification.valid:
        reason = "; ".join(verification.reasons)
        mark_invalid(manifest, reason, Path(snapshot))
        raise ManifestIntegrityError(reason)
    manifest["baseline_sealed"] = True
    manifest["sealed_at"] = _utc_timestamp()
    _write_manifest(snapshot, manifest)


def record_patched_file(
    manifest: dict[str, Any], resources: Path, target: Path | str | os.PathLike[str]
) -> None:
    if manifest_phase(manifest) != PHASE_SEALED:
        raise ManifestStateError("patched hashes require a sealed staging manifest")
    resources = Path(resources)
    candidate = Path(target)
    rel = resource_relative(candidate, resources) if candidate.is_absolute() else normalize_resources_relative_path(candidate)
    entry = next(
        (item for item in manifest["files"] if item.get("relative_path", item.get("path")) == rel),
        None,
    )
    if entry is None:
        raise ManifestStateError(f"patched file was not staged: {rel}")
    full = resolve_resources_path(resources, rel)
    if not full.is_file():
        raise ManifestIntegrityError(f"patched target missing: {rel}")
    entry["patched_sha256"] = sha256_file(full)


def commit_manifest(snapshot: Path, manifest: dict[str, Any]) -> None:
    if manifest_phase(manifest) != PHASE_SEALED:
        raise ManifestStateError("manifest must have a sealed baseline before commit")
    verification = verify_payload(snapshot, manifest)
    if not verification.valid:
        raise ManifestIntegrityError("; ".join(verification.reasons))
    missing = [item["relative_path"] for item in manifest["files"] if not item.get("patched_sha256")]
    if missing:
        raise ManifestStateError(f"patched hashes missing: {', '.join(missing)}")
    manifest["status"] = STATUS_COMPLETE
    manifest["state"] = STATUS_COMPLETE
    manifest["completed_at"] = _utc_timestamp()
    _write_manifest(snapshot, manifest)


def mark_complete(*args) -> dict[str, Any]:
    """Complete a manifest.

    ``mark_complete(manifest)`` is the in-memory state transition helper.
    ``mark_complete(app_dir, manifest, snapshot)`` also records every patched
    hash and persists the committed manifest.
    """
    if len(args) == 1:
        manifest = args[0]
        if manifest_phase(manifest) != PHASE_SEALED:
            raise ManifestStateError("manifest must have a sealed baseline before completion")
        missing = [item["relative_path"] for item in manifest["files"] if not item.get("patched_sha256")]
        if missing:
            raise ManifestStateError(f"patched hashes missing: {', '.join(missing)}")
        manifest["status"] = STATUS_COMPLETE
        manifest["state"] = STATUS_COMPLETE
        manifest["completed_at"] = _utc_timestamp()
        return manifest
    if len(args) != 3:
        raise TypeError("mark_complete expects manifest or app_dir, manifest, snapshot")
    app_dir, manifest, snapshot = args
    resources = Path(app_dir) / "resources"
    for entry in manifest["files"]:
        record_patched_file(manifest, resources, entry["relative_path"])
    commit_manifest(Path(snapshot), manifest)
    return manifest


def record_user_config(
    manifest: dict[str, Any],
    config_path: Path,
    *,
    locale: OriginalValue,
    font: OriginalValue,
    file_existed: bool | None = None,
) -> None:
    config_path = Path(config_path)
    manifest["user_config"] = {
        "path": str(config_path.resolve(strict=False)),
        "file_existed": config_path.is_file() if file_existed is None else bool(file_existed),
        "locale": locale.to_dict(),
        "font": font.to_dict(),
    }


def capture_user_config(manifest: dict[str, Any], config_path: Path) -> None:
    config_path = Path(config_path)
    existed = config_path.is_file()
    if existed:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManifestIntegrityError(f"cannot read Claude config: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestIntegrityError("Claude config root is not an object")
    else:
        data = {}
    record_user_config(
        manifest,
        config_path,
        locale=OriginalValue("locale" in data, data.get("locale")),
        font=OriginalValue("claudeZhCnFont" in data, data.get("claudeZhCnFont")),
        file_existed=existed,
    )


def restore_user_config(manifest: Mapping[str, Any], config_path: Path | None = None) -> bool:
    record = manifest.get("user_config")
    if not isinstance(record, dict) or not record:
        return True
    raw_path = config_path or record.get("path")
    if not raw_path:
        return False
    path = Path(raw_path)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
    else:
        data = {}
    for manifest_key, config_key in (("locale", "locale"), ("font", "claudeZhCnFont")):
        original = record.get(manifest_key, {})
        if original.get("existed"):
            data[config_key] = original.get("value")
        else:
            data.pop(config_key, None)
    if not record.get("file_existed") and not data:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return True
    except OSError:
        return False


def add_security_descriptor(
    manifest: dict[str, Any],
    *,
    target: Path,
    target_kind: str,
    owner: str | None,
    dacl: str | None,
    inheritance_enabled: bool | None,
    attributes: int | None,
) -> None:
    manifest["security_descriptors"].append(
        {
            "path": str(Path(target)),
            "target_kind": str(target_kind),
            "owner": owner,
            "dacl": dacl,
            "inheritance_enabled": inheritance_enabled,
            "attributes": attributes,
        }
    )


def load_matching_manifest(snapshots_root: Path, identity: Mapping[str, Any]) -> LoadedSnapshot:
    snapshot = Path(snapshots_root) / str(identity.get("install_id", ""))
    if not (snapshot / MANIFEST_FILE_NAME).is_file():
        raise ManifestMismatchError("no snapshot matches this Claude installation")
    manifest = load_manifest(snapshot)
    if manifest.get("identity") != dict(identity) or manifest.get("install_id") != identity.get("install_id"):
        raise ManifestMismatchError("snapshot identity does not match this Claude installation")
    if manifest_phase(manifest) != PHASE_COMPLETE or not manifest.get("baseline_sealed"):
        raise ManifestStateError("matching snapshot is not complete")
    verification = verify_payload(snapshot, manifest)
    if not verification.valid:
        raise ManifestIntegrityError("; ".join(verification.reasons))
    return LoadedSnapshot(snapshot, manifest, verification)


def validate_manifest(
    app_dir: Path,
    manifest: Mapping[str, Any],
    snapshot: Path,
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    value = _validate_manifest_shape(dict(manifest))
    identity = build_install_identity(app_dir)
    if value.get("identity") != identity or value.get("install_id") != identity["install_id"]:
        raise ManifestMismatchError("snapshot belongs to another Claude installation")
    if require_complete and manifest_phase(value) != PHASE_COMPLETE:
        raise ManifestStateError("snapshot is not complete")
    if not value.get("baseline_sealed"):
        raise ManifestStateError("snapshot baseline is not sealed")
    verification = verify_payload(snapshot, value)
    if not verification.valid:
        raise ManifestIntegrityError("; ".join(verification.reasons))
    return value


def create_sealed_snapshot(
    app_dir: Path,
    targets: Iterable[Path],
    *,
    config_path: Path | None = None,
    security_descriptors: Iterable[Mapping[str, Any]] = (),
    operations: Iterable[str] = (),
    privileged: bool | None = None,
    profile_id: str | None = None,
    patch_version: str = TOOL_VERSION,
) -> tuple[dict[str, Any], Path, bool]:
    app_dir = Path(app_dir)
    resources = app_dir / "resources"
    identity = build_install_identity(app_dir)
    if privileged is None:
        privileged = is_windowsapps_path(app_dir)
    root = select_snapshots_root(protected=bool(privileged))
    snapshot = root / identity["install_id"]
    requested = sorted({resource_relative(Path(path), resources) for path in targets})
    if (snapshot / MANIFEST_FILE_NAME).is_file():
        loaded = load_matching_manifest(root, identity)
        recorded = sorted(item["relative_path"] for item in loaded.manifest["files"])
        if recorded != requested:
            raise ManifestMismatchError("existing snapshot mutation plan differs from this tool")
        return loaded.manifest, loaded.snapshot_path, False
    if snapshot.exists():
        raise ManifestStateError("incomplete snapshot already exists")
    manifest = create_manifest(identity, patch_version=patch_version, profile_id=profile_id)
    manifest["operations"] = list(operations)
    manifest["security_descriptors"] = [dict(item) for item in security_descriptors]
    if config_path is not None:
        capture_user_config(manifest, config_path)
    for path in requested:
        stage_resource_file(snapshot, manifest, resources, path)
    seal_baseline(snapshot, manifest)
    return manifest, snapshot, True


def find_legacy_backups(base: Path) -> list[Path]:
    base = Path(base)
    found: set[Path] = set()
    for name in ("json-only", "chunks"):
        path = base / name
        if path.is_dir() and any(item.is_file() for item in path.rglob("*")):
            found.add(path)
    if base.is_dir():
        found.update(
            path
            for path in base.glob("Claude_*")
            if path.is_dir() and any(item.is_file() for item in path.rglob("*"))
        )
    return sorted(found, key=lambda item: item.as_posix().lower())


def backup_status(app_dir: Path) -> dict[str, str]:
    app_dir = Path(app_dir)
    protected = is_windowsapps_path(app_dir)
    root = select_snapshots_root(protected=protected)
    identity = build_install_identity(app_dir)
    try:
        load_matching_manifest(root, identity)
        return {"status": "ready", "detail": "matching complete snapshot"}
    except ManifestMismatchError:
        legacy = find_legacy_backups(root.parent)
        return {
            "status": "legacy_incompatible" if legacy else "missing",
            "detail": "legacy backups cannot be restored" if legacy else "no matching snapshot",
        }
    except ManifestStateError as exc:
        return {"status": "incomplete", "detail": str(exc)}
    except ManifestError as exc:
        return {"status": "corrupt", "detail": str(exc)}


def discard_incomplete_snapshot(snapshot: Path, *, expected_root: Path | None = None) -> bool:
    """Remove an unusable snapshot only when its exact root is known.

    This helper is intentionally fail-closed because its argument ultimately
    reaches ``shutil.rmtree``.  Snapshot ids are SHA-256 prefixes generated by
    this module; refusing any other name or parent prevents a bad caller from
    recursively deleting an unrelated directory.
    """
    snapshot = Path(snapshot)
    if expected_root is None or not re.fullmatch(r"[0-9a-f]{32}", snapshot.name):
        return False
    try:
        root = Path(expected_root).resolve(strict=False)
        if snapshot.parent.resolve(strict=False) != root:
            return False
        if not snapshot.is_dir() or snapshot.is_symlink():
            return False
        is_junction = getattr(snapshot, "is_junction", None)
        if callable(is_junction) and is_junction():
            return False
    except OSError:
        return False
    try:
        manifest = load_manifest(snapshot)
    except ManifestStateError:
        if (snapshot / MANIFEST_FILE_NAME).exists():
            return False
        # Directory without a manifest: a crash remnant left behind between
        # directory creation and the first manifest write. It can never be
        # loaded or restored, so removing it unblocks future installs.
        try:
            shutil.rmtree(snapshot)
            return True
        except OSError:
            return False
    except ManifestError:
        return False
    if manifest_phase(manifest) == PHASE_COMPLETE:
        return False
    try:
        shutil.rmtree(snapshot)
        return True
    except OSError:
        return False


__all__ = [name for name in globals() if not name.startswith("_")]
