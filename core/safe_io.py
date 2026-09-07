"""Crash-safe file helpers shared by install and restore transactions.

Every writer creates its temporary file beside the destination, flushes it,
copies filesystem metadata, and only then uses ``os.replace``.  Callers get an
exception on failure; this module never turns a failed write into success.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024
# Windows 上目标文件可能被尚未退出的 Claude、杀毒软件或索引器短暂占用，
# os.replace 此时抛 PermissionError(WinError 5/32)。短暂退避重试可以把
# 这类瞬时占用从“整个事务失败”降级为一次几乎无感的延迟。
_REPLACE_RETRY_DELAYS = (0.3, 0.6, 1.0, 1.5, 2.0)


def _replace_with_retry(temporary: Path, destination: Path) -> None:
    try:
        os.replace(temporary, destination)
        return
    except PermissionError as exc:
        last_error = exc
    for delay in _REPLACE_RETRY_DELAYS:
        time.sleep(delay)
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as exc:
            last_error = exc
    raise last_error


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = DEFAULT_HASH_CHUNK_SIZE) -> str:
    """Return the lowercase SHA-256 hex digest of *path* without loading it all."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flush_file(stream: BinaryIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _fsync_parent(path: Path) -> None:
    """Persist the directory entry where supported (best effort on Windows)."""
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        # The file itself has already been flushed.  Some filesystems do not
        # permit opening directories, so directory fsync is an extra guarantee.
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _metadata_source(destination: Path, fallback: Path | None) -> Path | None:
    if destination.exists():
        return destination
    if fallback is not None and fallback.exists():
        return fallback
    return None


def _temporary_acl(temporary: Path, *, restore: bool) -> None:
    """Adjust only our newly created temp file, never the parent or siblings."""
    command = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/icacls.exe"), str(temporary)]
    # OWNER RIGHTS applies to the file's owner (user or elevated Administrators),
    # without a localized account name or a grant to all users.
    command += ["/reset", "/q"] if restore else ["/grant:r", "*S-1-3-4:F", "/q"]
    result = subprocess.run(command, capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        error = OSError("Cannot restore temporary file inheritance" if restore else "Cannot prepare temporary file metadata access")
        error.io_step = "temporary_acl_restore" if restore else "temporary_acl_grant"
        error.command_exit = result.returncode
        raise error


def _copy_metadata(source: Path, temporary: Path) -> None:
    try:
        shutil.copystat(source, temporary, follow_symlinks=False)
    except PermissionError:
        if os.name != "nt":
            raise
        # A non-inheritable write grant on the parent permits mkstemp/write,
        # but its child can still inherit only read access. Reopening the child
        # for utime/chmod then fails. Grant its owner access just for metadata,
        # and reset to normal inherited permissions before the final rename.
        try:
            _temporary_acl(temporary, restore=False)
            shutil.copystat(source, temporary, follow_symlinks=False)
        finally:
            _temporary_acl(temporary, restore=True)


def _atomic_replace(
    destination: Path,
    writer: Callable[[BinaryIO], None],
    *,
    metadata_fallback: Path | None = None,
) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            writer(stream)
            _flush_file(stream)

        io_step = "metadata"
        source = _metadata_source(destination, metadata_fallback)
        if source is not None:
            _copy_metadata(source, temporary)

        io_step = "replace"
        _replace_with_retry(temporary, destination)
        _fsync_parent(destination.parent)
        return destination
    except BaseException as exc:
        if isinstance(exc, OSError) and not hasattr(exc, "io_step"):
            exc.io_step = locals().get("io_step", "write_temporary")
        # ``os.fdopen`` owns the descriptor after it succeeds.  If an unusual
        # failure happened before that point, close it here without hiding the
        # original exception.
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes | bytearray | memoryview) -> Path:
    """Atomically replace *path* with exactly *data*.

    Existing destination timestamps, mode bits and Windows file attributes are
    copied to the temporary file before replacement.  Security descriptors for
    protected WindowsApps targets are handled by the higher-level permission
    transaction because Python's portable stat API cannot represent a DACL.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    payload = bytes(data)
    return _atomic_replace(Path(path), lambda stream: stream.write(payload))


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> Path:
    """Encode *text* and atomically replace *path*."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    return atomic_write_bytes(path, text.encode(encoding, errors))


def atomic_copy(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> Path:
    """Atomically copy one regular file, including portable stat metadata."""
    source_path = Path(source)
    destination_path = Path(destination)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not source_path.is_file():
        raise FileNotFoundError(f"Source is not a regular file: {source_path}")

    def copy_to(stream: BinaryIO) -> None:
        with source_path.open("rb") as source_stream:
            shutil.copyfileobj(source_stream, stream, length=chunk_size)

    return _atomic_replace(
        destination_path,
        copy_to,
        metadata_fallback=source_path,
    )


__all__ = [
    "DEFAULT_HASH_CHUNK_SIZE",
    "atomic_copy",
    "atomic_write_bytes",
    "atomic_write_text",
    "sha256_file",
]
