from __future__ import annotations

import ctypes
import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from safe_io import atomic_copy, atomic_write_text


def is_windowsapps_path(path: Path) -> bool:
    if os.name != "nt":
        return False
    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        candidate = os.path.normcase(str(Path(path).resolve(strict=False)))
        windowsapps = os.path.normcase(str(root.resolve(strict=False)))
        return os.path.commonpath([candidate, windowsapps]) == windowsapps
    except ValueError:
        return False


def is_windows_admin() -> bool:
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(script_path: Path, script_args: Sequence[str]) -> bool:
    """Legacy CLI elevation helper; the GUI uses the restricted broker."""
    if os.name != "nt":
        return False
    parameters = subprocess.list2cmdline([str(script_path), *script_args])
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, parameters, str(script_path.parent), 0
        )
    except (AttributeError, OSError):
        return False
    return int(result) > 32


def ensure_admin_for_windowsapps(
    app_dir: Path, script_path: Path, script_args: Sequence[str]
) -> int | None:
    if not is_windowsapps_path(app_dir) or is_windows_admin():
        return None
    print("\n[!] WindowsApps 目标需要管理员权限。")
    if relaunch_as_admin(script_path, script_args):
        print("[OK] 已启动管理员进程。")
        return 0
    print("[X] 未获得管理员权限，未修改任何文件。")
    return 1


def print_permission_denied_hint(path: Path) -> None:
    print("\n[权限不足] 无法写入 Claude 安装文件：")
    print(f"  {path}")
    print("请完全关闭 Claude；WindowsApps 目标请使用正式安装版按需授权。")


def copy2_best_effort(src: Path, dst: Path, *, context: str) -> bool:
    """Atomic copy with one readonly-bit retry; failure is always visible."""
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
        except OSError as exc:
            print(f"Warning: cannot copy {context} from {src} to {dst}: {exc}")
            print_permission_denied_hint(dst)
            return False
    except OSError as exc:
        print(f"Warning: cannot copy {context} from {src} to {dst}: {exc}")
        return False


def write_text_best_effort(path: Path, text: str, *, context: str) -> bool:
    """Atomic UTF-8 write with one readonly-bit retry."""
    try:
        atomic_write_text(path, text)
        return True
    except PermissionError:
        if path.exists():
            try:
                path.chmod(path.stat().st_mode | stat.S_IWRITE)
            except OSError:
                pass
        try:
            atomic_write_text(path, text)
            return True
        except OSError as exc:
            print(f"Warning: cannot write {context} at {path}: {exc}")
            print_permission_denied_hint(path)
            return False
    except OSError as exc:
        print(f"Warning: cannot write {context} at {path}: {exc}")
        return False


@dataclass(frozen=True)
class SecurityDescriptorRecord:
    """Manifest-safe representation of a Windows owner/DACL snapshot."""

    path: str
    existed: bool
    sddl_b64: str | None = None
    owner_sid: str | None = None
    attributes: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "SecurityDescriptorRecord":
        return cls(
            path=str(value["path"]),
            existed=bool(value["existed"]),
            sddl_b64=value.get("sddl_b64"),
            owner_sid=value.get("owner_sid"),
            attributes=value.get("attributes"),
        )


@dataclass(frozen=True)
class PermissionPreparationOutcome:
    """Result of preparing one exact WindowsApps permission target."""

    success: bool
    step: str | None = None
    exit_code: int | None = None


def _powershell(args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if os.name == "nt":
        system_root = Path(env.get("SystemRoot", r"C:\Windows"))
        program_files = Path(env.get("ProgramFiles", r"C:\Program Files"))
        # A malformed user PSModulePath must not stop security cmdlets from
        # loading inside a frozen/UTF-8 Python process.
        env["PSModulePath"] = os.pathsep.join(
            (
                str(system_root / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"),
                str(program_files / "WindowsPowerShell" / "Modules"),
            )
        )
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
    )


def _relative_to_resources(path: Path, resources: Path) -> str:
    try:
        rel = Path(path).resolve(strict=False).relative_to(resources.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"permission target escapes resources: {path}") from exc
    if not rel.parts:
        # Creating resources/zh-CN.json requires access to this exact parent.
        # It is still a bounded target, not a recursive ownership operation.
        return "."
    return rel.as_posix()


def capture_security_descriptor(path: Path, resources: Path) -> SecurityDescriptorRecord:
    """Capture owner, DACL and attributes before any ACL mutation."""
    path = Path(path)
    rel = _relative_to_resources(path, resources)
    if not path.exists():
        return SecurityDescriptorRecord(path=rel, existed=False)
    if os.name != "nt":
        return SecurityDescriptorRecord(path=rel, existed=True, attributes=path.stat().st_mode)

    escaped = str(path.resolve(strict=False)).replace("'", "''")
    script = (
        f"$p='{escaped}';$i=Get-Item -LiteralPath $p -Force;$a=Get-Acl -LiteralPath $p;"
        "$s=$a.GetSecurityDescriptorSddlForm([System.Security.AccessControl.AccessControlSections]::All);"
        "$o=$a.Owner;try{$o=([System.Security.Principal.NTAccount]$o).Translate([System.Security.Principal.SecurityIdentifier]).Value}catch{};"
        "[pscustomobject]@{sddl=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($s));owner=$o;attributes=[int]$i.Attributes}|ConvertTo-Json -Compress"
    )
    result = _powershell(["-Command", script])
    if result.returncode != 0:
        raise PermissionError(result.stderr.strip() or f"cannot capture ACL: {path}")
    try:
        value = json.loads(result.stdout.strip())
        return SecurityDescriptorRecord(
            path=rel,
            existed=True,
            sddl_b64=str(value["sddl"]),
            owner_sid=str(value["owner"]),
            attributes=int(value["attributes"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OSError(f"invalid ACL capture for {path}") from exc


def restore_security_descriptor(record: SecurityDescriptorRecord, resources: Path) -> bool:
    path = resources / Path(record.path)
    if not record.existed:
        if not path.exists() or os.name != "nt":
            return True
        try:
            result = subprocess.run(
                ["icacls", str(path), "/reset", "/q"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    if not path.exists():
        return False
    if os.name != "nt":
        if record.attributes is not None:
            path.chmod(record.attributes)
        return True
    if not record.sddl_b64:
        return False
    escaped_path = str(path.resolve(strict=False)).replace("'", "''")
    escaped_sddl = record.sddl_b64.replace("'", "''")
    attributes = int(record.attributes or 0)
    script = (
        f"$p='{escaped_path}';$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{escaped_sddl}'));"
        # Restore attributes while the temporary administrator ACL is still
        # active.  Set-Acl may revoke that access (the normal WindowsApps
        # state), so it must be the final operation in this PowerShell call.
        f"(Get-Item -LiteralPath $p -Force).Attributes={attributes};"
        "$a=Get-Acl -LiteralPath $p;$a.SetSecurityDescriptorSddlForm($s,[System.Security.AccessControl.AccessControlSections]::All);"
        "Set-Acl -LiteralPath $p -AclObject $a"
    )
    result = _powershell(["-Command", script])
    return result.returncode == 0


def _prepare_exact_windowsapps_target(path: Path) -> PermissionPreparationOutcome:
    """Prepare one exact target and retain only safe, structured failure data."""
    if os.name != "nt":
        return PermissionPreparationOutcome(True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        own = subprocess.run(
            # SID form avoids a localized group-name lookup.  Unlike takeown,
            # this has no recursive-only /D option to accidentally combine
            # with an exact, non-recursive target.
            ["icacls", str(path), "/setowner", "*S-1-5-32-544", "/q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=flags,
        )
        if own.returncode != 0:
            # Some WindowsApps ACLs reject icacls /setowner even from an
            # elevated administrator token. takeown enables the dedicated
            # ownership privilege, so use it as a non-recursive fallback.
            take = subprocess.run(
                ["takeown", "/f", str(path), "/a"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=flags,
            )
            if take.returncode != 0:
                return PermissionPreparationOutcome(False, "take_ownership", take.returncode)
        grant = subprocess.run(
            ["icacls", str(path), "/grant:r", "*S-1-5-32-544:F", "/c", "/q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=flags,
        )
        if grant.returncode != 0:
            return PermissionPreparationOutcome(False, "grant_admin", grant.returncode)
        return PermissionPreparationOutcome(True)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return PermissionPreparationOutcome(False, "command_start")


def prepare_exact_windowsapps_target(path: Path) -> bool:
    """Grant Administrators access to one exact file/directory; never recurse."""
    return _prepare_exact_windowsapps_target(path).success


class PermissionTransaction:
    """Capture, prepare and restore exact targets from a sealed plan."""

    def __init__(self, app_dir: Path, targets: Sequence[Path]) -> None:
        self.app_dir = Path(app_dir)
        self.resources = self.app_dir / "resources"
        exact: dict[str, Path] = {}
        for target in targets:
            target = Path(target)
            permission_target = target if target.exists() else target.parent
            exact[_relative_to_resources(permission_target, self.resources)] = permission_target
            # atomic_copy/atomic_write create a sibling temporary file and
            # then call os.replace(). On Windows that operation requires
            # create/delete rights on the direct parent as well as rights on
            # the destination file itself.
            parent = permission_target.parent
            try:
                parent_rel = _relative_to_resources(parent, self.resources)
            except ValueError:
                # resources itself is the bounded root; never prepare its
                # parent, which would escape the sealed transaction scope.
                pass
            else:
                exact[parent_rel] = parent
        self.targets = [exact[key] for key in sorted(exact)]
        self.records: list[SecurityDescriptorRecord] = []
        self._prepared: list[SecurityDescriptorRecord] = []
        self._failure: PermissionPreparationOutcome | None = None
        self._failure_record: SecurityDescriptorRecord | None = None
        self.progress_cb = None

    def _progress(self, current: int, total: int) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(current, total)
            except Exception:
                pass

    def capture(self) -> list[dict]:
        self.records = []
        for index, path in enumerate(self.targets, 1):
            self._progress(index - 1, len(self.targets))
            self.records.append(capture_security_descriptor(path, self.resources))
            self._progress(index, len(self.targets))
        return [record.to_dict() for record in self.records]

    def prepare(self) -> bool:
        # A failed patch still has the first transaction's temporary ACLs in
        # place. Replaying prepare() used to duplicate records and then
        # restore the same ACL twice, causing a false rollback failure.
        if self._prepared:
            return True
        self._failure = None
        self._failure_record = None
        if not is_windowsapps_path(self.app_dir):
            return True
        if not is_windows_admin() or not self.records:
            self._failure = PermissionPreparationOutcome(False, "admin_check")
            return False
        for path, record in zip(self.targets, self.records, strict=True):
            # Record before changing ownership. If the owner change succeeds
            # but the subsequent grant fails, restore() must still return this
            # exact target to its captured SDDL.
            self._prepared.append(record)
            outcome = _prepare_exact_windowsapps_target(path)
            self._progress(len(self._prepared), len(self.targets))
            if not outcome.success:
                self._failure = outcome
                self._failure_record = record
                self.restore()
                return False
        return True

    def diagnostic_context(self) -> dict[str, str | int]:
        """Return redacted, relative metadata for a failed prepare attempt."""
        if self._failure is None:
            return {}
        context: dict[str, str | int] = {}
        if self._failure.step:
            context["permission_step"] = self._failure.step
        if isinstance(self._failure.exit_code, int):
            context["command_exit"] = self._failure.exit_code
        if self._failure_record is not None:
            context["resource"] = self._failure_record.path
        return context

    def restore(self) -> bool:
        ok = True
        for index, record in enumerate(reversed(self._prepared), 1):
            ok = restore_security_descriptor(record, self.resources) and ok
            self._progress(index, len(self._prepared))
        self._prepared.clear()
        return ok


def ensure_windowsapps_writable(app_dir: Path) -> bool:
    """Legacy shim: protected targets require a sealed PermissionTransaction."""
    return not is_windowsapps_path(Path(app_dir))


__all__ = [
    "PermissionTransaction",
    "PermissionPreparationOutcome",
    "SecurityDescriptorRecord",
    "capture_security_descriptor",
    "copy2_best_effort",
    "ensure_admin_for_windowsapps",
    "ensure_windowsapps_writable",
    "is_windows_admin",
    "is_windowsapps_path",
    "prepare_exact_windowsapps_target",
    "print_permission_denied_hint",
    "relaunch_as_admin",
    "restore_security_descriptor",
    "write_text_best_effort",
]
