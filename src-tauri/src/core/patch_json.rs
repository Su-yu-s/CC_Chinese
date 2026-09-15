// patch_json.rs — JSON 资源补丁 + locale 设置
// 对应 Python 版 core/patch_json.py
//
// 职责：
// 1. 校验内置 zh-CN 资源非空
// 2. 备份既有文件到 LocalAppData（修改前）
// 3. 原子写入三份 zh-CN JSON 资源
// 4. 白名单正则插入 zh-CN 语言数组
// 5. 设置用户 config 的 locale = "zh-CN"
use crate::core::safe_io::{atomic_write_bytes, atomic_write_text, sha256_hex};
use crate::core::errors::{CcError, Result};
use serde_json::Value;
use std::path::{Path, PathBuf};

/// 工具自身状态文件（补丁完成后写入，恢复时删除）
pub fn patch_state_path() -> PathBuf {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    PathBuf::from(local).join("CC_Chinese").join("state").join("patch-state.json")
}

/// 校验内置翻译资源（嵌入二进制）非空
pub fn validate_source_resources() -> Result<()> {
    let checks = [
        ("desktop", DESKTOP_ZH_CN, 20_000),
        ("frontend", FRONTEND_ZH_CN, 1_000_000),
        ("statsig", STATSIG_ZH_CN, 1_000),
    ];
    for (name, content, min_len) in &checks {
        if content.len() < *min_len {
            return Err(CcError::Generic(format!(
                "内置 {name} 资源大小异常: {} < {min_len}",
                content.len()
            )));
        }
        // 确认是合法 JSON
        serde_json::from_str::<Value>(content).map_err(|e| {
            CcError::Generic(format!("内置 {name} 资源 JSON 解析失败: {e}"))
        })?;
    }
    Ok(())
}

/// 备份既有文件到 LocalAppData（不依赖快照，是第二道保险）
fn backup_file(src: &Path) -> Result<()> {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    let backup_root = PathBuf::from(local).join("CC_Chinese").join("backups");
    std::fs::create_dir_all(&backup_root)
        .map_err(|e| CcError::Generic(format!("创建备份目录: {e}")))?;
    let rel = src
        .to_string_lossy()
        .replace(':', "")
        .replace('\\', "_");
    let dst = backup_root.join(format!("backup_{rel}"));
    let bytes = std::fs::read(src).map_err(|e| CcError::Generic(format!("读取 {src:?}: {e}")))?;
    atomic_write_bytes(&dst, &bytes)
}

/// 执行 JSON 补丁主流程
/// 返回修改的文件数（相对路径列表），供上层记录 manifest
pub fn run_patch(app_dir: &Path, _include_statsig: bool) -> Result<Vec<String>> {
    validate_source_resources()?;
    let resources = app_dir.join("resources");
    if !resources.is_dir() {
        return Err(CcError::Generic(format!("resources 目录不存在: {resources:?}")));
    }

    let mut modified: Vec<String> = Vec::new();

    // 1. 写入 desktop-zh-CN.json
    let desktop_zh = resources.join("ion-dist/i18n/zh-CN.json");
    let desktop_target = desktop_zh.exists()
        .then(|| desktop_zh.to_path_buf())
        .or_else(|| Some(resources.join("zh-CN.json")));
    if let Some(target) = desktop_target {
        if target.exists() {
            backup_file(&target)?;
        }
        let rel = target
            .strip_prefix(&resources)
            .ok()
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        std::fs::create_dir_all(target.parent().unwrap_or(&target))
            .map_err(|e| CcError::Generic(format!("创建目录 {target:?}: {e}")))?;
        let json_str = {
            let v: Value = serde_json::from_str::<Value>(DESKTOP_ZH_CN)
                .map_err(|e| CcError::Generic(format!("内置 desktop 资源 JSON 解析失败: {e}")))?;
            serde_json::to_string_pretty(&v)
                .map_err(|e| CcError::Generic(format!("内置 desktop 资源序列化失败: {e}")))?
        };
        atomic_write_bytes(&target, json_str.as_bytes())?;
        modified.push(rel);
    }

    // 2. 写入 frontend-zh-CN.json（en-US.json 旁边的 zh-CN.json）
    let frontend_zh = resources.join("ion-dist/i18n/en-US.json");
    let frontend_zh_target = resources.join("ion-dist/i18n/zh-CN.json");
    if frontend_zh.is_file() {
        // 备份 frontend 语言文件
        if frontend_zh_target.exists() {
            backup_file(&frontend_zh_target)?;
        }
        std::fs::create_dir_all(frontend_zh_target.parent().unwrap_or(&frontend_zh_target))
            .map_err(|e| CcError::Generic(format!("创建目录: {e}")))?;
        let json_str = {
            let v: Value = serde_json::from_str::<Value>(FRONTEND_ZH_CN)
                .map_err(|e| CcError::Generic(format!("内置 frontend 资源 JSON 解析失败: {e}")))?;
            serde_json::to_string_pretty(&v)
                .map_err(|e| CcError::Generic(format!("内置 frontend 资源序列化失败: {e}")))?
        };
        atomic_write_bytes(&frontend_zh_target, json_str.as_bytes())?;
        let rel = frontend_zh_target
            .strip_prefix(&resources)
            .ok()
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        modified.push(rel);

        // 3. 插入 zh-CN 到语言白名单数组
        let zh_in_whitelist = insert_zh_in_locale_array(&frontend_zh);
        if zh_in_whitelist {
            // en-US.json 本身被修改了，也备份
            backup_file(&frontend_zh)?;
            let rel = frontend_zh
                .strip_prefix(&resources)
                .ok()
                .map(|p| p.to_string_lossy().replace('\\', "/"))
                .unwrap_or_default();
            if !modified.contains(&rel) {
                modified.push(rel);
            }
        }
    }

    // 4. 写入 statsig-zh-CN.json
    let statsig_zh = resources.join("ion-dist/i18n/statsig-zh-CN.json");
    if statsig_zh.exists() {
        backup_file(&statsig_zh)?;
    }
    std::fs::create_dir_all(statsig_zh.parent().unwrap_or(&statsig_zh))
        .map_err(|e| CcError::Generic(format!("创建目录: {e}")))?;
    let json_str = {
        let v: Value = serde_json::from_str::<Value>(STATSIG_ZH_CN)
            .map_err(|e| CcError::Generic(format!("内置 statsig 资源 JSON 解析失败: {e}")))?;
        serde_json::to_string_pretty(&v)
            .map_err(|e| CcError::Generic(format!("内置 statsig 资源序列化失败: {e}")))?
    };
    atomic_write_bytes(&statsig_zh, json_str.as_bytes())?;
    let rel = statsig_zh
        .strip_prefix(&resources)
        .ok()
        .map(|p| p.to_string_lossy().replace('\\', "/"))
        .unwrap_or_default();
    modified.push(rel);

    // 5. 写工具状态文件
    let state = serde_json::json!({
        "modified": modified,
        "fingerprint": sha256_hex(DESKTOP_ZH_CN.as_bytes()),
    });
    let state_path = patch_state_path();
    if let Some(parent) = state_path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    atomic_write_bytes(
        &state_path,
        state.to_string().as_bytes(),
    )?;

    // 6. 设置用户 config 的 locale
    let _ = set_locale();

    Ok(modified)
}

/// 向 locale 数组插入 "zh-CN"（白名单机制）
/// 简单实现：查找 "en-US" 所在的语言数组，插入 "zh-CN"
fn insert_zh_in_locale_array(file: &Path) -> bool {
    let content = match std::fs::read_to_string(file) {
        Ok(c) => c,
        Err(_) => return false,
    };
    let value: Value = match serde_json::from_str(&content) {
        Ok(v) => v,
        Err(_) => return false,
    };
    if let Some(obj) = value.as_object() {
        let needs_insert: bool = obj.values().any(|v| {
            v.as_array().map(|arr| {
                arr.iter().any(|item| item.as_str() == Some("en-US"))
                    && !arr.iter().any(|item| item.as_str() == Some("zh-CN"))
            }) == Some(true)
        });
        if needs_insert {
            let value = serde_json::Value::Object(
                obj.iter()
                    .map(|(k, v)| {
                        if let Some(mut arr) = v.as_array().cloned() {
                            if arr.iter().any(|item| item.as_str() == Some("en-US"))
                                && !arr.iter().any(|item| item.as_str() == Some("zh-CN"))
                            {
                                arr.push(serde_json::Value::String("zh-CN".into()));
                                (k.clone(), serde_json::Value::Array(arr))
                            } else {
                                (k.clone(), v.clone())
                            }
                        } else {
                            (k.clone(), v.clone())
                        }
                    })
                    .collect(),
            );
            if let Ok(text) = serde_json::to_string(&value) {
                let _ = crate::core::safe_io::atomic_write_text(file, &text);
                return true;
            }
        }
    }
    false
}

/// 设置用户 config 的 locale = "zh-CN"（仅修改该键，不动其它内容）
fn set_locale() -> Result<()> {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    let config = PathBuf::from(local).join("Claude-3p").join("config.json");
    if !config.is_file() {
        return Ok(()); // 配置缺失在干净安装上是正常态
    }
    let text = std::fs::read_to_string(&config)
        .map_err(|e| CcError::Generic(format!("读取 config {config:?}: {e}")))?;
    let mut value: Value = serde_json::from_str(&text)
        .map_err(|e| CcError::Generic(format!("解析 config {config:?}: {e}")))?;
    if let Some(obj) = value.as_object_mut() {
        obj.insert("locale".into(), Value::String("zh-CN".into()));
    }
    let out = serde_json::to_string_pretty(&value)
        .map_err(|e| CcError::Generic(format!("序列化 config: {e}")))?;
    atomic_write_text(&config, &out)
}

/// 清理工具自身状态文件（恢复后调用）
pub fn cleanup_state() {
    let _ = std::fs::remove_file(patch_state_path());
}

// 内嵌翻译资源（编译时打进二进制，运行时解出写盘）
macro_rules! embed_json {
    ($path:expr) => {
        include_str!(concat!(env!("CARGO_MANIFEST_DIR"), $path))
    };
}

const DESKTOP_ZH_CN: &str = embed_json!("/src/resources/desktop-zh-CN.json");
const FRONTEND_ZH_CN: &str = embed_json!("/src/resources/frontend-zh-CN.json");
const STATSIG_ZH_CN: &str = embed_json!("/src/resources/statsig-zh-CN.json");
