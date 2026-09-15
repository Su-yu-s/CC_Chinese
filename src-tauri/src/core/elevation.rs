// elevation.rs — UAC broker：ShellExecuteExW(runas) + nonce 一次性结果文件
// 对应 Python 版 core/elevation.py
//
// 不变量：
// - 动作集恒为 {patch, restore}，父/子两侧独立校验 action + nonce 格式
// - 父进程对 WindowsApps 只做纯路径前缀校验；深度校验（reparse/en-US.json/ACL）
//   必须在提权子进程内做
// - 结果文件一次性：读取后立即删除，仅回传白名单 4 字段
use crate::core::proc::hidden_command;
use crate::core::acl::is_windowsapps_path;
use crate::core::installer::{self, OpResult};
use crate::core::safe_io::atomic_write_text;
use std::path::{Path, PathBuf};

/// 允许的动作集
const ALLOWED_ACTIONS: &[&str] = &["patch", "restore"];

/// nonce 格式：32 位小写十六进制
fn is_valid_nonce(nonce: &str) -> bool {
    nonce.len() == 32 && nonce.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

/// 生成随机 nonce
pub fn new_nonce() -> String {
    // 用时间戳 + 进程 ID 混合作文件名唯一性（非密码学随机，够用）
    use std::time::{SystemTime, UNIX_EPOCH};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() % 0xFFFF_FFFF_FFFF)
        .unwrap_or(0);
    let pid = std::process::id() as u128;
    let mixed = pid ^ now.wrapping_mul(0x9E37_79B9);
    format!("{mixed:032x}")
}

/// 提权结果文件目录
fn elevation_result_dir() -> PathBuf {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    PathBuf::from(local).join("CC_Chinese").join("elevation-results")
}

/// 写结果文件（白名单 4 字段，一次性）
fn write_result(nonce: &str, result: &OpResult) -> std::result::Result<PathBuf, String> {
    if !is_valid_nonce(nonce) {
        return Err("invalid elevation nonce".into());
    }
    let dir = elevation_result_dir();
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let payload = serde_json::json!({
        "success": result.success,
        "state": result.state.chars().take(32).collect::<String>(),
        "error_code": result.error_code.as_deref().unwrap_or("").chars().take(64).collect::<String>(),
        "message": result.message.chars().take(500).collect::<String>(),
    });
    let path = dir.join(format!("{nonce}.json"));
    atomic_write_text(&path, &payload.to_string()).map_err(|e| e.to_string())?;
    Ok(path)
}

/// 读结果文件（读取后立即删除）
fn read_result(nonce: &str) -> Option<OpResult> {
    if !is_valid_nonce(nonce) {
        return None;
    }
    let path = elevation_result_dir().join(format!("{nonce}.json"));
    let raw = std::fs::read_to_string(&path).ok()?;
    let value: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let result = OpResult {
        success: value.get("success").and_then(|v| v.as_bool()).unwrap_or(false),
        state: value
            .get("state")
            .and_then(|v| v.as_str())
            .unwrap_or("error")
            .to_string(),
        message: value
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        error_code: value
            .get("error_code")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string()),
        diagnostic_context: None,
    };
    let _ = std::fs::remove_file(&path);
    Some(result)
}

/// 检查路径是否含 reparse point（junction/symlink）
/// 必须用未 resolve 的路径做检查，resolve 会跟随它本要检测的 reparse point。
fn has_reparse_component(path: &Path) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        // 逐层向上检查父路径（不用 canonicalize，避免跟随符号链接）
        let mut p = Path::new(path);
        loop {
            if let Ok(meta) = std::fs::symlink_metadata(p) {
                if meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                    return true;
                }
            }
            match p.parent() {
                Some(parent) if !parent.as_os_str().is_empty() => p = parent,
                _ => break,
            }
        }
        false
    }
    #[cfg(not(windows))]
    {
        false
    }
}

/// 校验 WindowsApps 目标（深度校验，必须在提权后执行）
fn validated_windowsapps_target(app_dir: &Path) -> Option<PathBuf> {
    let raw = std::fs::canonicalize(app_dir).ok()?;
    if has_reparse_component(&raw) {
        return None;
    }
    if !is_windowsapps_path(&raw) {
        return None;
    }
    if !raw.join("resources/en-US.json").is_file() {
        return None;
    }
    Some(raw)
}

/// 提权子进程入口：校验后调 installer，结果写 nonce 文件
/// 返回进程退出码
pub fn run_elevated_cli(action: &str, target_hint: &str, nonce: &str) -> i32 {
    // 双侧校验
    if !ALLOWED_ACTIONS.contains(&action) || !is_valid_nonce(nonce) {
        return 2;
    }
    if !crate::core::acl::is_admin() {
        return 3;
    }
    let target = match validated_windowsapps_target(Path::new(target_hint)) {
        Some(t) => t,
        None => {
            let _ = write_result(
                nonce,
                &OpResult {
                    success: false,
                    state: "error".into(),
                    message: "提权环境未通过完整性检查。".into(),
                    error_code: Some("ELEVATION_TAMPERED".into()),
                    diagnostic_context: None,
                },
            );
            return 4;
        }
    };

    let result = if action == "patch" {
        installer::run_install(target.to_string_lossy().as_ref(), true)
    } else {
        installer::run_restore(target.to_string_lossy().as_ref(), true)
    };

    let _ = write_result(nonce, &result);
    if result.success { 0 } else { 1 }
}

/// 父进程侧：发起 UAC 提权并等待结果
/// 返回结果（失败时为 ELEVATION_TAMPERED）
pub fn launch_elevated_and_wait(action: &str, app_dir: &Path) -> OpResult {
    if !ALLOWED_ACTIONS.contains(&action) {
        return OpResult {
            success: false,
            state: "error".into(),
            message: "非法提权动作。".into(),
            error_code: Some("ELEVATION_TAMPERED".into()),
            diagnostic_context: None,
        };
    }
    if !is_windowsapps_path(app_dir) {
        return OpResult {
            success: false,
            state: "error".into(),
            message: "目标未通过 WindowsApps 路径校验。".into(),
            error_code: Some("ELEVATION_TAMPERED".into()),
            diagnostic_context: None,
        };
    }

    let nonce = new_nonce();
    let exe = std::env::current_exe().unwrap_or_default();
    let target_str = app_dir.to_string_lossy().to_string();

    // 拉起已提权的自身副本
    // 用 start /runas 等价：ShellExecuteExW(runas)
    // Rust 没有直接 std 实现，用 powershell 兜底（开发阶段）
    let _ = hidden_command("powershell")
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "Start-Process -FilePath '{}' -ArgumentList '--elevated-action {} --target-hint {} --nonce {}' -Verb RunAs -Wait",
                exe.display(),
                action,
                target_str.replace(' ', "` "),
                nonce,
            ),
        ])
        .output();

    // 等待结果文件（子进程写入）
    for _ in 0..120 {
        if let Some(result) = read_result(&nonce) {
            return result;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }

    OpResult {
        success: false,
        state: "error".into(),
        message: "提权进程未返回有效结果。".into(),
        error_code: Some("ELEVATION_TAMPERED".into()),
        diagnostic_context: None,
    }
}
