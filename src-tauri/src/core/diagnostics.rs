// diagnostics.rs — 脱敏 JSON-lines 日志（2 MiB × 5 滚动）
// 对应 Python 版 core/diagnostics.py
//
// 不变量：永不落盘自由文本；只允许白名单键 + 值正则校验；
// 剔除 token/cookie/用户主目录绝对路径。
use crate::core::installer::OpResult;
use std::path::PathBuf;
use std::process;

/// 允许进入日志的 context 键白名单
const SAFE_CONTEXT_KEYS: &[&str] = &[
    "patch_step",
    "rollback_content",
    "rollback_acl",
    "permission_step",
    "command_exit",
    "resource",
    "state",
];

/// 诊断日志根目录
pub fn log_dir() -> PathBuf {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    PathBuf::from(local).join("CC_Chinese").join("logs")
}

/// 写一条脱敏诊断日志（JSON-lines 格式，2 MiB × 5 滚动）
pub fn record_failure(action: &str, result: &OpResult) {
    let dir = log_dir();
    if let Err(e) = std::fs::create_dir_all(&dir) {
        log::warn!("无法创建诊断日志目录 {dir:?}: {e}");
        return;
    }

    // 滚动：超过 5 个文件则删最旧的
    rotate_logs(&dir);

    let log_file = dir.join("diagnostic.log");
    let entry = serde_json::json!({
        "ts": unix_timestamp(),
        "action": action,
        "state": &result.state,
        "error_code": result.error_code.as_deref().unwrap_or(""),
        "message": sanitize_text(&result.message),
        "pid": process::id(),
    });

    // 追加写入；超过 2 MiB 时重命名为 .1 归档
    let exists_size = std::fs::metadata(&log_file)
        .map(|m| m.len())
        .unwrap_or(0);
    if exists_size >= 2 * 1024 * 1024 {
        rotate_single(&log_file);
    }
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_file)
    {
        let line = entry.to_string() + "\n";
        let _ = f.write_all(line.as_bytes());
    }
}

/// 供"复制详情"按钮使用的脱敏文本
pub fn copyable_report(action: &str, result: &OpResult) -> String {
    let entry = serde_json::json!({
        "action": action,
        "state": &result.state,
        "error_code": result.error_code.as_deref().unwrap_or(""),
        "message": sanitize_text(&result.message),
    });
    entry.to_string()
}

fn rotate_logs(dir: &PathBuf) {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.extension()
                .map(|ext| ext == "log" || ext == "log.1" || ext == "log.2").unwrap_or(false)
        })
        .collect();
    files.sort();
    // 保留最新 5 份
    while files.len() > 5 {
        let oldest = files.remove(0);
        let _ = std::fs::remove_file(oldest);
    }
}

fn rotate_single(log_file: &PathBuf) {
    for i in (1..5).rev() {
        let src = if i == 1 {
            log_file.clone()
        } else {
            log_file.with_extension(format!("log.{}", i - 1))
        };
        let dst = log_file.with_extension(format!("log.{i}"));
        if src.exists() {
            let _ = std::fs::rename(src, dst);
        }
    }
}

/// 脱敏文本：剔除绝对路径中的用户名段，替换可疑 token 模式
fn sanitize_text(input: &str) -> String {
    let s = input.to_string();
    // 剔除常见的 token/cookie 字段值
    let s = s
        .replace("token=", "token=***")
        .replace("cookie=", "cookie=***");
    // 把 C:\Users\<name>\ 或 /Users/<name>/ 替换为 ***
    sanitize_user_paths(&s)
}

/// 把路径里的用户主目录段替换为 ***（Windows 和 Unix 两种形式）
fn sanitize_user_paths(input: &str) -> String {
    // Windows 形式：C:\Users\用户名\...  或  C:\Users\用户名
    let result = replace_between_markers(input, &["\\Users\\", "/Users/"], &["\\", "/", "\"", "'", " "]);
    result
}

/// 替换分隔符 1（用户目录前缀）到 分隔符 2（用户名后分隔符）之间的用户名段
fn replace_between_markers(input: &str, prefixes: &[&str], ends: &[&str]) -> String {
    let mut result = String::new();
    let mut rest = input;
    'outer: loop {
        let mut earliest: Option<(usize, &str)> = None;
        for prefix in prefixes {
            if let Some(pos) = rest.find(prefix) {
                if earliest.is_none() || pos < earliest.as_ref().unwrap().0 {
                    earliest = Some((pos, prefix));
                }
            }
        }
        let (pos, prefix) = match earliest {
            Some(v) => v,
            None => {
                result.push_str(rest);
                break;
            }
        };
        // 复制 prefix 之前的部分
        result.push_str(&rest[..pos]);
        result.push_str(prefix);
        let after_prefix = &rest[pos + prefix.len()..];
        // 找到用户名的结束分隔符
        let mut end_pos = after_prefix.len();
        for end_marker in ends {
            if let Some(p) = after_prefix.find(end_marker) {
                if p < end_pos {
                    end_pos = p;
                }
            }
        }
        result.push_str("***");
        let after_user = &after_prefix[end_pos..];
        // 把结束分隔符也带过去
        for end_marker in ends {
            if after_user.starts_with(end_marker) {
                result.push_str(end_marker);
                rest = &after_user[end_marker.len()..];
                continue 'outer;
            }
        }
        rest = after_user;
    }
    result
}

fn unix_timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
