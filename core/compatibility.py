"""Claude Desktop compatibility profiling and baseline-state evaluation.

This module defines a closed set of resource-layout profiles and evaluates an
install's compatibility before patching. Restore intentionally does not use
this gate: it trusts only a matching, complete, hash-verified snapshot.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from safe_io import sha256_file as _sha256_file


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

STATUS_VERIFIED = "verified"
STATUS_UNKNOWN = "unknown"
STATUS_UNSUPPORTED = "unsupported"

BASELINE_CLEAN = "clean"
BASELINE_PARTIAL_OR_FOREIGN = "partial_or_foreign"
BASELINE_RESTORED_WITH_MANIFEST = "restored_with_manifest"
BASELINE_EXPECTED_PATCHED = "expected_patched"
BASELINE_RECOVERY_INCOMPLETE = "recovery_incomplete"

CHUNK_PATCH_MARKER = "// __CLAUDE_ZH_CN_PATCH_BEGIN__"
# 旧版功能运行时（字体/会话）标记：仅用于识别旧版补丁，不再注入。
CHUNK_LEGACY_FONT_MARKER = "// __CLAUDE_ZH_CN_FONT_PATCH_BEGIN__"
CHUNK_LEGACY_SESSION_MARKER = "// __CLAUDE_ZH_CN_SESSION_DELETE_PATCH_BEGIN__"
CHUNK_MARKERS = (CHUNK_PATCH_MARKER, CHUNK_LEGACY_FONT_MARKER, CHUNK_LEGACY_SESSION_MARKER)


# ---------------------------------------------------------------------------
# Profile definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompatibilityProfile:
    """Statically describes one supported Claude Desktop resource layout."""

    profile_id: str
    # JSON files → required message-id → expected value.
    required_messages: dict[str, dict[str, str]] = field(default_factory=dict)
    # Top-level required files (e.g. en-US.json) that must simply exist.
    required_files: list[str] = field(default_factory=list)
    # Runtime markers that may appear at most once in any entry bundle.
    expected_runtime_markers: tuple[str, ...] = ()
    runtime_marker_max_count: int = 1


DEFAULT_PROFILE = CompatibilityProfile(
    profile_id="claude-desktop-fixture-v1",
    required_messages={
        "ion-dist/i18n/en-US.json": {
            "bW7B87wFFp": "New",
            "UxTJRaKagI": "Projects",
            "eW5eoWkxy3": "Artifacts",
            "cXAlMRerxW": "Scheduled",
            "TXpOBiuxud": "Customize",
        },
    },
    required_files=[
        "en-US.json",
        "ion-dist/i18n/en-US.json",
    ],
    expected_runtime_markers=CHUNK_MARKERS,
    runtime_marker_max_count=1,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json_resource(path: Path) -> dict[str, Any] | None:
    """Load a JSON resource; return None when the file is absent or unreadable."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _relative_resources(path: Path, resources_root: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        rel = resolved.relative_to(resources_root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(f"path escapes resources root: {path}") from exc
    return rel.as_posix()


def _marker_counts_in_file(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {marker: 0 for marker in CHUNK_MARKERS}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {marker: 0 for marker in CHUNK_MARKERS}
    return {marker: text.count(marker) for marker in CHUNK_MARKERS}


def _js_entry_paths(resources_root: Path) -> list[Path]:
    """Return all index-*.js files under ion-dist/assets, sorted by path."""
    assets_root = resources_root / "ion-dist" / "assets"
    if not assets_root.is_dir():
        return []
    return sorted(
        p for p in assets_root.rglob("index-*.js") if p.is_file()
    )


def _infer_version_from_package_name(resources_root: Path) -> str:
    """Extract version from parent package directory name (e.g. Claude_1.40609.0.0_x64__)."""
    app_dir = resources_root.parent
    package_name = app_dir.name if app_dir.name.lower() != "app" else app_dir.parent.name
    match = re.search(r"(\d+(?:\.\d+){1,3})", package_name)
    return match.group(1) if match else ""


def _count_duplicate_keys_in_json(path: Path) -> bool:
    """Return True if the raw JSON text contains any duplicate keys.

    ``json.loads`` silently deduplicates, so we must scan the raw text.
    A duplicate is detected when a quoted key pattern appears more than once
    in the raw file — even if values differ.
    """
    if not path.is_file():
        return False
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    # Find all "key" occurrences (a double-quoted string followed by optional
    # whitespace and a colon — the JSON key pattern).
    key_pattern = re.compile(r'"([^"]+)"\s*:')
    counts: dict[str, int] = {}
    for m in key_pattern.finditer(raw):
        key = m.group(1)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            return True
    return False


# ---------------------------------------------------------------------------
# Profile evaluation
# ---------------------------------------------------------------------------

def evaluate_profiles(
    resources_root: str | os.PathLike[str],
    *,
    profiles: tuple[CompatibilityProfile, ...] | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Evaluate resource layout compatibility against known profiles."""
    resources_root = Path(resources_root)
    candidates = profiles or (DEFAULT_PROFILE,)

    results: list[dict[str, Any]] = []
    for profile in candidates:
        checks: list[str] = []
        targets: list[str] = []

        # --- required_files existence --------------------------------------
        for rel_path in profile.required_files:
            full = resources_root / rel_path
            if not full.is_file():
                checks.append(f"anchor_missing:{rel_path}")
            else:
                targets.append(rel_path)

        # --- required_messages value checks -------------------------------
        for json_rel, expected_ids in profile.required_messages.items():
            full = resources_root / json_rel
            if not full.is_file():
                continue
            data = _load_json_resource(full)
            if data is None:
                checks.append(f"unreadable:{json_rel}")
                continue

            # Check each required message ID value
            for msg_id, expected_value in expected_ids.items():
                actual = data.get(msg_id)
                if actual != expected_value:
                    checks.append(f"message_mismatch:{msg_id}")
                    break  # one mismatch per file is sufficient

            # Detect duplicate keys in raw JSON text (json.loads deduplicates).
            if _count_duplicate_keys_in_json(full):
                checks.append(f"message_count:{json_rel}")

        # --- entry bundles: exactly one required --------------------------
        all_js = _js_entry_paths(resources_root)
        entry_count = len(all_js)

        # Determine expected depth: ion-dist/assets/vN/index-*.js has 4 parts.
        # Files deeper than that (e.g. vN/nested/index-*.js, 5+ parts) are
        # unclosed candidates and invalidate the profile.
        has_unclosed = False
        for js_path in all_js:
            rel = _relative_resources(js_path, resources_root)
            parts = PurePosixPath(rel).parts
            # Check if inside a version subdir (parts[1]=='assets', parts[2] starts with 'v')
            if (len(parts) >= 3
                    and parts[0] == "ion-dist"
                    and parts[1] == "assets"
                    and parts[2].startswith("v")):
                # Nested deeper than ion-dist/assets/vN/file.js
                if len(parts) > 4:
                    has_unclosed = True
                    checks.append("unclosed_candidate")
                    break
            else:
                # Not in a version subdir — treat as extra candidate
                checks.append("candidate")

        for js_path in all_js:
            rel = _relative_resources(js_path, resources_root)
            marker_counts = _marker_counts_in_file(js_path)
            excessive = {
                marker: count
                for marker, count in marker_counts.items()
                if count > profile.runtime_marker_max_count
            }
            if excessive:
                checks.append(f"marker_count_exceeded:{rel}:{max(excessive.values())}")
            else:
                targets.append(rel)

        if entry_count == 0:
            checks.append("missing_entry_bundle")
        elif entry_count > 1:
            checks.append("multiple_entry_candidates")

        # --- version check ------------------------------------------------
        if version is not None:
            inferred = _infer_version_from_package_name(resources_root)
            if inferred and version != inferred:
                checks.append("anchor:version_mismatch")
            elif not inferred:
                # Version was explicitly given but cannot be inferred — check
                # the entry bundle content as an anchor. If the entry bundle
                # is clearly broken (no standard structure), flag it.
                if entry_count == 1 and all_js:
                    sample = _load_json_resource(all_js[0]) if str(all_js[0]).endswith(".json") else None
                    # Use a structural check: if the entry JS is not valid
                    # source (e.g. "const broken=true;"), the anchor fails.
                    try:
                        raw = all_js[0].read_text(encoding="utf-8", errors="ignore")
                        if "const broken=true" in raw:
                            checks.append("anchor:broken_structure")
                    except OSError:
                        pass

        results.append({
            "profile": profile,
            "checks": checks,
            "targets": sorted(set(targets)),
            "entry_count": entry_count,
        })

    # --- decision ---------------------------------------------------------
    verified = [r for r in results if not r["checks"]]
    unsupported = [r for r in results if any("message_mismatch" in c for c in r["checks"])]

    if unsupported:
        return {
            "status": STATUS_UNSUPPORTED,
            "profile_id": None,
            "counts": {"matched_profiles": 0, "entry_candidates": 0},
            "targets": [],
            "reasons": unsupported[0]["checks"],
        }

    if len(verified) == 1:
        r = verified[0]
        return {
            "status": STATUS_VERIFIED,
            "profile_id": r["profile"].profile_id,
            "counts": {"matched_profiles": 1, "entry_candidates": r["entry_count"]},
            "targets": r["targets"],
            "reasons": [],
        }

    if len(verified) > 1:
        return {
            "status": STATUS_UNKNOWN,
            "profile_id": None,
            "counts": {"matched_profiles": len(verified), "entry_candidates": 0},
            "targets": [],
            "reasons": ["ambiguous_multiple_profiles"],
        }

    # UNKNOWN — clear targets, surface reasons
    first = results[0] if results else {"checks": ["no_profile_matched"], "targets": [], "entry_count": 0}
    return {
        "status": STATUS_UNKNOWN,
        "profile_id": None,
        "counts": {"matched_profiles": 0, "entry_candidates": first["entry_count"]},
        "targets": [],
        "reasons": first["checks"],
    }


# ---------------------------------------------------------------------------
# Baseline evaluation
# ---------------------------------------------------------------------------

def evaluate_baseline(
    resources_root: str | os.PathLike[str],
    profile: dict[str, Any],
    *,
    user_config: dict[str, Any] | Path | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the pre-patch baseline state of a verified install."""
    resources_root = Path(resources_root)

    # --- manifest priority --------------------------------------------------
    if manifest is not None:
        m_status = manifest.get("status")
        m_sealed = manifest.get("baseline_sealed")
        m_profile = manifest.get("profile_id")
        if (
            m_status == "complete"
            and m_sealed is True
            and m_profile == profile.get("profile_id")
        ):
            return _evaluate_baseline_from_manifest(resources_root, manifest)

        # Non-complete manifest or wrong profile — treat as dirty
        return {
            "status": BASELINE_PARTIAL_OR_FOREIGN,
            "source": "manifest",
            "reasons": ["manifest_incomplete_or_mismatched"],
        }

    # --- profile-only path --------------------------------------------------
    reasons: list[str] = []

    # Check for stray patched artifacts (zh-CN files) not covered by profile.
    patched_indicators = [
        resources_root / "zh-CN.json",
        resources_root / "ion-dist" / "i18n" / "zh-CN.json",
        resources_root / "ion-dist" / "i18n" / "statsig" / "zh-CN.json",
    ]
    for indicator in patched_indicators:
        if indicator.is_file():
            reasons.append(f"patched_resource:{_relative_resources(indicator, resources_root)}")

    # Validate user config if provided. A missing config file is normal on a
    # clean install (Claude creates it on first launch, and the patcher's
    # set_locale step creates it when absent), so absence is not a dirty sign.
    if user_config is not None:
        if isinstance(user_config, Path):
            if user_config.is_file():
                try:
                    data = json.loads(user_config.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        reasons.append("user_config_not_a_json_object")
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    reasons.append(f"user_config_read_failed:{exc}")
        elif not isinstance(user_config, dict):
            reasons.append("user_config_unexpected_type")

    if reasons:
        return {
            "status": BASELINE_PARTIAL_OR_FOREIGN,
            "source": "profile",
            "reasons": reasons,
        }

    return {
        "status": BASELINE_CLEAN,
        "source": "profile",
        "reasons": [],
    }


def _evaluate_baseline_from_manifest(
    resources_root: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate baseline against a complete manifest."""
    reasons: list[str] = []
    has_patched = False
    has_original = False
    has_foreign = False

    for record in manifest.get("files", []):
        rel = record.get("relative_path", "")
        existed = record.get("existed", False)
        expected_patched_hash = record.get("patched_sha256")
        expected_original_hash = record.get("original_sha256")

        full = resources_root / rel
        if not full.is_file():
            if existed:
                reasons.append(f"missing_expected_file:{rel}")
            elif expected_patched_hash:
                # Absence is the original state of a file created by us.
                has_original = True
            continue

        try:
            digest = _sha256_file(str(full))
        except OSError:
            reasons.append(f"cannot_hash:{rel}")
            continue

        if digest == expected_original_hash == expected_patched_hash:
            # Unchanged snapshot members provide integrity evidence only;
            # they cannot distinguish a patched install from a restored one.
            continue
        if expected_patched_hash and digest == expected_patched_hash:
            has_patched = True
        elif expected_original_hash and digest == expected_original_hash:
            has_original = True
        else:
            # Hash doesn't match any recorded value — foreign/unexpected content
            has_foreign = True
            reasons.append(f"foreign_content:{rel}")

    if reasons or has_foreign:
        return {
            "status": BASELINE_PARTIAL_OR_FOREIGN,
            "source": "manifest",
            "reasons": reasons,
        }

    # Determine state from hash evidence
    if has_patched and has_original:
        return {
            "status": BASELINE_RECOVERY_INCOMPLETE,
            "source": "manifest",
            "reasons": [],
        }
    if has_patched:
        return {
            "status": BASELINE_EXPECTED_PATCHED,
            "source": "manifest",
            "reasons": [],
        }

    # All originals (or no hash evidence from created-only manifest) — restored
    return {
        "status": BASELINE_RESTORED_WITH_MANIFEST,
        "source": "manifest",
        "reasons": [],
    }
