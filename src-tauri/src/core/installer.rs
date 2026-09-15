// installer.rs — 密封事务编排：run_install / run_restore / run_open
// 对应 Python 版 core/installer.py
//
// 核心不变量：
// - 写前封存：快照/prepare 失败则绝不改 Claude 任何文件
// - 回滚源唯一：Sealed 快照是唯一可信回滚源，绝不删除/覆盖/续写
// - 成功判据全量：只有全量 _verify_localized 通过且 manifest 提交 complete 才成功
// - 失败立即整体回滚（快照文件 + ACL）
use crate::core::acl::PermissionTransaction;
use crate::core::backup_manifest;
use crate::core::compatibility::{self, CompatStatus};
use crate::core::detector;
use crate::core::errors::CcError;
use crate::core::patch_chunks;
use crate::core::patch_json;
use crate::core::safe_io::sha256_file;
use serde::Serialize;
use std::path::Path;

pub const TOOL_VERSION: &str = "1.0.0";

#[derive(Serialize, Clone, Debug)]
pub struct OpResult {
    pub success: bool,
    pub state: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub diagnostic_context: Option<serde_json::Value>,
}

fn op_failure(code: &str, msg: &str, state: &str) -> OpResult {
    OpResult {
        success: false,
        state: state.into(),
        message: msg.into(),
        error_code: Some(code.into()),
        diagnostic_context: None,
    }
}

fn op_success(state: &str, msg: &str) -> OpResult {
    OpResult {
        success: true,
        state: state.into(),
        message: msg.into(),
        error_code: None,
        diagnostic_context: None,
    }
}

/// 收集补丁目标文件（相对路径）
fn target_plan(resources: &Path) -> Vec<String> {
    let mut targets = vec![
        "ion-dist/i18n/en-US.json".to_string(),
        "ion-dist/i18n/zh-CN.json".to_string(),
        "en-US.json".to_string(),
    ];
    // 追加所有入口 JS
    for js in patch_chunks::collect_chunk_mutation_targets(resources) {
        if let Ok(rel) = js.strip_prefix(resources) {
            targets.push(rel.to_string_lossy().replace('\\', "/"));
        }
    }
    targets.sort();
    targets.dedup();
    targets
}

/// 全量校验汉化结果
fn verify_localized(app_dir: &Path) -> (bool, String) {
    let resources = app_dir.join("resources");
    // 检查 patch marker 至少存在于一处入口 JS
    let has_marker = patch_chunks::has_complete_runtime_markers(&resources);
    if !has_marker {
        return (false, "未找到补丁指纹标记，汉化未生效".into());
    }
    (true, "OK".into())
}

/// 恢复快照文件到基线
fn restore_snapshot_files(
    resources: &Path,
    snapshot_dir: &Path,
    manifest: &backup_manifest::Manifest,
) -> bool {
    for (rel, record) in manifest.files.iter() {
        let target = resources.join(rel);
        if !record.existed {
            // 该文件原本不存在，恢复 = 删除
            let _ = std::fs::remove_file(&target);
            continue;
        }
        let payload = snapshot_dir.join(backup_manifest::payload_rel_for(rel));
        let target = resources.join(rel);
        let bytes = match std::fs::read(&payload) {
            Ok(b) => b,
            Err(e) => {
                log::warn!("快照 payload 读取失败 {rel}: {e}");
                return false;
            }
        };
        if let Err(e) = crate::core::safe_io::atomic_write_bytes(&target, &bytes) {
            log::warn!("快照还原写入失败 {rel}: {e}");
            return false;
        }
    }
    true
}

/// 执行汉化（密封事务）
pub fn run_install(app_dir_str: &str, elevated: bool) -> OpResult {
    let app_dir = std::path::PathBuf::from(app_dir_str);
    let resources = app_dir.join("resources");
    if !resources.is_dir() {
        return op_failure("LOCATE_NOT_FOUND", "未找到 Claude Desktop 安装目录。", "missing");
    }

    let protected = crate::core::acl::is_windowsapps_path(&app_dir);
    let include_statsig = !protected;

    // 1. 校验内置资源
    if let Err(e) = patch_json::validate_source_resources() {
        return op_failure("SOURCE_INVALID", &format!("内置资源校验失败: {e}"), "error");
    }

    // 2. 权限检查
    if protected && (!elevated || !crate::core::acl::is_admin()) {
        return op_failure(
            "PERMISSION_DENIED",
            "WindowsApps 版本需要管理员授权后才能汉化。",
            "error",
        );
    }

    // 3. 兼容性评估
    let report = compatibility::evaluate(&resources);
    if report.status != CompatStatus::Verified {
        let code = match report.status {
            CompatStatus::Unsupported => "COMPAT_UNSUPPORTED",
            _ => "COMPAT_UNKNOWN",
        };
        return op_failure(code, &report.reason, "compat_error");
    }
    if report.baseline == compatibility::Baseline::Dirty {
        return op_failure(
            "BASELINE_DIRTY",
            "检测到不完整或来源不明的修改，未继续汉化。",
            "baseline_dirty",
        );
    }
    // Patched 状态 = 已汉化过，直接返回 ready
    if report.baseline == compatibility::Baseline::Patched {
        let (ok, detail) = verify_localized(&app_dir);
        if ok {
            return op_success("ready", "当前版本已经完成汉化，无需重复处理。");
        }
        return op_failure("PATCH_VERIFY_FAILED", &detail, "error");
    }

    // 4. 收集目标
    let targets = target_plan(&resources);
    if targets.is_empty() {
        return op_failure("COMPAT_UNKNOWN", "当前版本没有可验证的补丁目标。", "compat_error");
    }

    // 5. 关闭 Claude 进程
    match detector::stop_claude(12) {
        Ok(()) => {}
        Err(e) => {
            return op_failure(
                "CLAUDE_PROCESS_ALIVE",
                &format!("无法关闭 Claude 进程，请手动关闭后重试: {e}"),
                "error",
            );
        }
    }

    // 6. 加载已有快照或创建新快照
    let mut manifest = match backup_manifest::load_existing_snapshot(&app_dir, protected) {
        Ok(Some(m)) => {
            let existing_targets: Vec<String> = m.files.keys().cloned().collect();
            if existing_targets != targets {
                return op_failure(
                    "SNAPSHOT_MISMATCH",
                    "现有快照与当前补丁计划不匹配。",
                    "error",
                );
            }
            m
        }
        Ok(None) => match backup_manifest::create_sealed_snapshot(&app_dir, &targets, protected) {
            Ok((_, m)) => m,
            Err(e) => {
                return op_failure(
                    "SNAPSHOT_CREATE_FAILED",
                    &format!("安全基线创建失败，未修改 Claude: {e}"),
                    "error",
                );
            }
        },
        Err(e) => {
            return op_failure(
                "SNAPSHOT_INVALID",
                &format!("现有快照不完整: {e}"),
                "error",
            );
        }
    };

    // 7. 权限事务
    let mut permissions = PermissionTransaction::new(app_dir.clone(), targets.iter());
    if protected {
        permissions.capture();
        if !permissions.prepare() {
            let ctx = permissions.diagnostic_context();
            return OpResult {
                success: false,
                state: "error".into(),
                message: "无法安全获取目标文件写权限。".into(),
                error_code: Some("PERMISSION_DENIED".into()),
                diagnostic_context: Some(ctx),
            };
        }
    }

    // 8. 打补丁
    let result = (|| -> Result<(), CcError> {
        // JSON 补丁
        let json_modified = patch_json::run_patch(&app_dir, include_statsig)?;
        // JS chunk 补丁
        let chunk_result = patch_chunks::apply_chunks_with_cache(&app_dir, &resources)?;
        // 记录补丁哈希到 manifest
        for rel in json_modified.iter().chain(chunk_result.target_files.iter()) {
            let patched = resources.join(rel);
            if patched.is_file() {
                let hash = sha256_file(&patched)?;
                let hex = crate::core::safe_io::sha256_hex(&hash);
                backup_manifest::record_patched_file(&mut manifest, rel, &hex)?;
            }
        }
        // 提交 manifest
        let snapshot_dir = backup_manifest::snapshot_dir_for(&app_dir, protected);
        backup_manifest::commit_manifest(&snapshot_dir, &mut manifest)?;
        Ok(())
    })();

    match result {
        Ok(()) => {
            if !permissions.restore() {
                return op_failure(
                    "ACL_RESTORE_FAILED",
                    "汉化完成，但目标文件权限未能完整还原。",
                    "ready",
                );
            }
            op_success("ready", "汉化完成")
        }
        Err(e) => {
            // 回滚
            let snapshot_dir = backup_manifest::snapshot_dir_for(&app_dir, protected);
            let rollback_ok = restore_snapshot_files(&resources, &snapshot_dir, &manifest);
            let acl_ok = permissions.restore();
            patch_json::cleanup_state();
            patch_chunks::cleanup_state();
            let code = if rollback_ok && acl_ok {
                "PATCH_WRITE_FAILED"
            } else {
                "PATCH_ROLLBACK_FAILED"
            };
            let msg = if rollback_ok && acl_ok {
                format!("汉化未完成，已恢复修改前状态: {e}")
            } else {
                format!("汉化失败且自动回滚未完整完成: {e}")
            };
            OpResult {
                success: false,
                state: "error".into(),
                message: msg,
                error_code: Some(code.into()),
                diagnostic_context: None,
            }
        }
    }
}

/// 从快照恢复官方原状
pub fn run_restore(app_dir_str: &str, elevated: bool) -> OpResult {
    let app_dir = std::path::PathBuf::from(app_dir_str);
    let resources = app_dir.join("resources");
    if !resources.is_dir() {
        return op_failure("LOCATE_NOT_FOUND", "未找到 Claude Desktop 安装目录。", "missing");
    }
    let protected = crate::core::acl::is_windowsapps_path(&app_dir);

    if protected && (!elevated || !crate::core::acl::is_admin()) {
        return op_failure(
            "PERMISSION_DENIED",
            "WindowsApps 版本需要管理员授权后才能恢复。",
            "error",
        );
    }

    if let Err(e) = detector::stop_claude(12) {
        return op_failure(
            "CLAUDE_PROCESS_ALIVE",
            &format!("无法关闭 Claude 进程: {e}"),
            "error",
        );
    }

    // 加载版本匹配的完整快照
    let (snapshot_dir, manifest) = match backup_manifest::load_matching_manifest(&app_dir, protected) {
        Ok(v) => v,
        Err(e) => {
            return op_failure("SNAPSHOT_INVALID", &format!("无法加载有效快照: {e}"), "error");
        }
    };

    // 恢复前校验：目标文件哈希必须是 known 值（original 或 patched）
    for (rel, record) in manifest.files.iter() {
        if !record.existed {
            continue;
        }
        let target = resources.join(rel);
        if !target.is_file() {
            continue;
        }
        let hash = match crate::core::safe_io::sha256_file(&target) {
            Ok(h) => h,
            Err(_) => continue,
        };
        let hex = crate::core::safe_io::sha256_hex(&hash);
        let known = record.original_sha256.as_deref() == Some(&hex)
            || record.patched_sha256.as_deref() == Some(&hex);
        if !known {
            return op_failure(
                "BASELINE_DIRTY",
                "目标文件已被第三方修改，拒绝恢复。请先使用 Claude 官方修复/重装。",
                "baseline_dirty",
            );
        }
    }

    // 权限事务
    let mut permissions =
        PermissionTransaction::new(app_dir.clone(), manifest.files.keys());
    if protected {
        permissions.capture();
        if !permissions.prepare() {
            return op_failure("PERMISSION_DENIED", "无法获取写权限。", "error");
        }
    }

    // 恢复快照文件
    let ok = restore_snapshot_files(&resources, &snapshot_dir, &manifest);
    let acl_ok = permissions.restore();
    patch_json::cleanup_state();
    patch_chunks::cleanup_state();

    if ok && acl_ok {
        op_success("restored", "已恢复官方原状")
    } else {
        op_failure("RESTORE_FAILED", "恢复未完整完成，请检查权限后重试。", "error")
    }
}

/// 启动 Claude Desktop
pub fn run_open(app_dir_str: &str) -> OpResult {
    let app_dir = std::path::PathBuf::from(app_dir_str);
    match detector::open_claude(&app_dir) {
        Ok(()) => op_success("open", "Claude Desktop 已启动"),
        Err(e) => op_failure("OPEN_FAILED", &format!("无法启动 Claude: {e}"), "error"),
    }
}
