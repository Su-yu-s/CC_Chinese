// compatibility.rs — 基线洁净度评估（clean/dirty/patched/unknown/unsupported）
// 对应 Python 版 core/compatibility.py
use serde::Serialize;
use std::path::Path;

/// 基线状态
#[derive(Serialize, Clone, Copy, PartialEq, Debug)]
#[serde(rename_all = "snake_case")]
pub enum Baseline {
    Clean,
    Dirty,
    Patched,
    Unknown,
}

/// 兼容性状态
#[derive(Serialize, Clone, Copy, PartialEq, Debug)]
#[serde(rename_all = "snake_case")]
pub enum CompatStatus {
    Verified,
    Unknown,
    Unsupported,
}

#[derive(Serialize, Clone, Debug)]
pub struct CompatReport {
    pub status: CompatStatus,
    pub baseline: Baseline,
    pub profile: String,
    pub reason: String,
}

/// 当前支持的 Claude 版本锚点（与 Python 版 DEFAULT_PROFILE 一致）
const REQUIRED_FILES: &[&str] = &["en-US.json", "ion-dist/i18n/en-US.json"];
const ANCHOR_MESSAGES: &[(&str, &str, &str)] = &[
    ("ion-dist/i18n/en-US.json", "bW7B87wFFp", "New"),
    ("ion-dist/i18n/en-US.json", "UxTJRaKagI", "Projects"),
    ("ion-dist/i18n/en-US.json", "eW5eoWkxy3", "Artifacts"),
    ("ion-dist/i18n/en-US.json", "cXAlMRerxW", "Scheduled"),
    ("ion-dist/i18n/en-US.json", "TXpOBiuxud", "Customize"),
];
const PATCH_MARKER: &str = "__CLAUDE_ZH_CN_PATCH_BEGIN__";

/// 评估 resources_root 的布局兼容性。
/// 返回 Verified + Clean/Patched 才允许写入；其它状态一律拒绝。
pub fn evaluate(resources_root: &Path) -> CompatReport {
    let profile = "claude-desktop-fixture-v1".to_string();

    // 1. 必需文件存在
    for rel in REQUIRED_FILES {
        if !resources_root.join(rel).is_file() {
            return CompatReport {
                status: CompatStatus::Unsupported,
                baseline: Baseline::Unknown,
                profile,
                reason: format!("缺少必需文件: {rel}"),
            };
        }
    }

    // 2. 锚点消息值校验
    let i18n = resources_root.join("ion-dist/i18n/en-US.json");
    if let Ok(raw) = std::fs::read_to_string(&i18n) {
        if let Ok(data) = serde_json::from_str::<serde_json::Value>(&raw) {
            for (_file, msg_id, expected) in ANCHOR_MESSAGES {
                let actual = data.get(msg_id).and_then(|v| v.as_str());
                if actual != Some(expected) {
                    return CompatReport {
                        status: CompatStatus::Unsupported,
                        baseline: Baseline::Dirty,
                        profile,
                        reason: format!("锚点消息 {msg_id} 值不符: 期望 {expected}, 实际 {actual:?}"),
                    };
                }
            }
        }
    }

    // 3. 基线洁净度：检查是否含 patch marker（= 已被本工具打过补丁）
    let baseline = check_baseline(resources_root);

    CompatReport {
        status: CompatStatus::Verified,
        baseline,
        profile,
        reason: "布局已验证".into(),
    }
}

/// 检查基线是 Clean / Patched / Dirty
fn check_baseline(resources_root: &Path) -> Baseline {
    // 检查 en-US.json 是否含 patch marker（正常官方文件绝不含）
    let en_us = resources_root.join("en-US.json");
    if let Ok(content) = std::fs::read(&en_us) {
        if content.windows(PATCH_MARKER.len()).any(|w| w == PATCH_MARKER.as_bytes()) {
            return Baseline::Dirty; // 官方 en-US.json 里出现 marker = 被外部污染
        }
    }

    // 检查 JS entry bundles 是否已有 patch marker
    if let Some(js_dir) = find_entry_js(resources_root) {
        let any_patched = js_dir.iter().any(|p| {
            std::fs::read(p)
                .map(|content| {
                    content
                        .windows(PATCH_MARKER.len())
                        .any(|w| w == PATCH_MARKER.as_bytes())
                })
                .unwrap_or(false)
        });
        if any_patched {
            return Baseline::Patched;
        }
    }

    Baseline::Clean
}

/// 查找 ion-dist/assets 下的入口 JS 文件（index-*.js）
fn find_entry_js(resources_root: &Path) -> Option<Vec<std::path::PathBuf>> {
    let assets = resources_root.join("ion-dist/assets");
    if !assets.is_dir() {
        return None;
    }
    let mut found = Vec::new();
    walk_js_files(&assets, &mut found);
    (!found.is_empty()).then_some(found)
}

fn walk_js_files(dir: &Path, out: &mut Vec<std::path::PathBuf>) {
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                walk_js_files(&p, out);
            } else if p.file_name().and_then(|n| n.to_str()).map_or(false, |n| n.starts_with("index-") && n.ends_with(".js")) {
                out.push(p);
            }
        }
    }
}
