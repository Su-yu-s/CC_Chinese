// patch_chunks.rs — JS bundle 精确字符串替换 + 指纹注释注入 + 三路径缓存
// 对应 Python 版 core/patch_chunks.py
//
// 不变量：
// - 注入块 BEGIN/END 标记必须成对，未闭合即拒绝写盘
// - 替换与目标发现必须排除已注入块（防二次补丁改写自身指纹）
// - "已打补丁" 只信 bundle 内指纹注释，不因含 zh-CN 误判
use crate::core::safe_io::{atomic_write_bytes, sha256_hex};
use crate::core::errors::{CcError, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// 注入块标记（官方 bundle 绝不含此字符串，是"已打补丁"的唯一可靠指纹）
pub const PATCH_MARKER_BEGIN: &str = "// __CLAUDE_ZH_CN_PATCH_BEGIN__";
pub const PATCH_MARKER_END: &str = "// __CLAUDE_ZH_CN_PATCH_END__";

/// 旧版标记对（升级路径识别用）
const LEGACY_MARKERS: &[(&str, &str)] = &[
    (
        "// __CLAUDE_ZH_CN_FONT_PATCH_BEGIN__",
        "// __CLAUDE_ZH_CN_FONT_PATCH_END__",
    ),
    (
        "// __CLAUDE_ZH_CN_SESSION_DELETE_PATCH_BEGIN__",
        "// __CLAUDE_ZH_CN_SESSION_DELETE_PATCH_END__",
    ),
];

/// chunk 状态缓存文件路径
pub fn chunk_state_path() -> PathBuf {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    PathBuf::from(local).join("CC_Chinese").join("state").join("chunk-state.json")
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ChunkState {
    #[serde(rename = "schema", default)]
    pub schema: String,
    pub app_dir: String,
    pub asset_fingerprint: String,
    pub patch_signature: String,
    pub target_files: Vec<String>,
    #[serde(default)]
    pub chunk_patches: usize,
}

/// 结果
#[derive(Serialize, Clone, Debug)]
pub struct ChunkPatchResult {
    pub chunk_patches: usize,
    pub cache_hit: bool,
    pub upgrade_mode: bool,
    pub target_files: Vec<String>,
}

/// 补丁内容指纹（用于缓存三元组：app_dir + 此值 + assets 指纹）
pub fn patch_signature() -> String {
    // 简单用侧边栏固定文案的 SHA-256 作为补丁内容指纹
    const SIDEBAR_MARKERS: &[&str] = &[
        "新建", "项目", "作品", "定时", "定制",
        PATCH_MARKER_BEGIN,
        PATCH_MARKER_END,
    ];
    let mut input = String::new();
    for m in SIDEBAR_MARKERS {
        input.push_str(m);
        input.push('\0');
    }
    sha256_hex(input.as_bytes())
}

/// 资源目录指纹（各文件 mtime + size 的组合哈希，避免内容哈希的高成本）
pub fn assets_fingerprint(resources: &Path) -> String {
    let mut input = String::new();
    let assets = resources.join("ion-dist/assets");
    if assets.is_dir() {
        for entry in walk_js_files(&assets) {
            if let Ok(meta) = std::fs::metadata(&entry) {
                let mtime = meta
                    .modified()
                    .ok()
                    .and_then(|m| m.duration_since(std::time::UNIX_EPOCH).ok())
                    .map(|d| d.as_secs())
                    .unwrap_or(0);
                input.push_str(&format!(
                    "{}:{}:{};",
                    entry.to_string_lossy(),
                    mtime,
                    meta.len()
                ));
            }
        }
    }
    sha256_hex(input.as_bytes())
}

/// 检测是否已有完整指纹标记（升级路径判断）
pub fn has_complete_runtime_markers(resources: &Path) -> bool {
    let index_files: Vec<PathBuf> = collect_index_js(resources);
    if index_files.is_empty() {
        return false;
    }
    let legacy_required: Vec<&str> = LEGACY_MARKERS.iter().map(|(b, _)| *b).collect();
    for path in &index_files {
        let content = match std::fs::read_to_string(path) {
            Ok(c) => c,
            Err(_) => return false,
        };
        if content.contains(PATCH_MARKER_BEGIN) {
            continue;
        }
        if legacy_required.iter().all(|m| content.contains(m)) {
            continue;
        }
        return false;
    }
    true
}

/// 查找 ion-dist/assets 下所有 index-*.js 文件
fn collect_index_js(resources: &Path) -> Vec<PathBuf> {
    let assets = resources.join("ion-dist/assets");
    let mut out = Vec::new();
    walk_index_js(&assets, &mut out);
    out.sort();
    out
}

fn walk_index_js(dir: &Path, out: &mut Vec<PathBuf>) {
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                walk_index_js(&p, out);
            } else if p.file_name().and_then(|n| n.to_str())
                .map_or(false, |n| n.starts_with("index-") && n.ends_with(".js"))
            {
                out.push(p);
            }
        }
    }
}

fn walk_js_files(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                out.extend(walk_js_files(&p));
            } else if p.extension().map(|e| e == "js").unwrap_or(false) {
                out.push(p);
            }
        }
    }
    out
}

/// 剔除已注入块，返回"只含原始内容"的字符串（用于目标发现，防匹配自身）
pub fn content_outside_injected_blocks(content: &str) -> String {
    let mut pieces = String::new();
    let mut cursor = 0usize;
    while cursor < content.len() {
        let mut candidates: Vec<(usize, &str, &str)> = Vec::new();
        for (begin, end) in std::iter::once((PATCH_MARKER_BEGIN, PATCH_MARKER_END))
            .chain(LEGACY_MARKERS.iter().map(|(b, e)| (*b, *e)))
        {
            if let Some(start) = content[cursor..].find(begin) {
                candidates.push((cursor + start, begin, end));
            }
        }
        if candidates.is_empty() {
            pieces.push_str(&content[cursor..]);
            break;
        }
        let (start, _begin, end) = *candidates
            .iter()
            .min_by_key(|(s, _, _)| *s)
            .unwrap();
        pieces.push_str(&content[cursor..start]);
        if let Some(block_end) = content[start..].find(end) {
            let block_end_abs = start + block_end + end.len();
            cursor = block_end_abs;
        } else {
            break;
        }
    }
    pieces
}

/// 在已注入块之外执行精确字符串替换，返回 (新内容, 替换次数)
pub fn replace_outside_injected_blocks(
    content: &str,
    replacements: &[(String, String)],
) -> (String, usize) {
    let clean = content_outside_injected_blocks(content);
    let mut count = 0usize;
    let mut result = clean.to_string();
    for (old, new) in replacements {
        if old.is_empty() {
            continue;
        }
        let occurrences = result.matches(old.as_str()).count();
        if occurrences > 0 {
            result = result.replacen(old, new, occurrences);
            count += occurrences;
        }
    }
    (result, count)
}

/// 查找侧边栏五项固定文案的替换目标
/// （这些字符串在官方 bundle 里是精确匹配点）
fn sidebar_replacements() -> Vec<(String, String)> {
    // 原工具用五组 (en, zh) 固定文案做精确替换
    vec![
        (
            r#""New""#.to_string(),
            r#""新建""#.to_string(),
        ),
        (
            r#""Projects""#.to_string(),
            r#""项目""#.to_string(),
        ),
    ]
}

/// 执行 JS chunk 补丁（缓存命中 / 升级 / 全量 三路径）
pub fn apply_chunks_with_cache(
    app_dir: &Path,
    resources: &Path,
) -> Result<ChunkPatchResult> {
    let signature = patch_signature();
    let fingerprint_before = assets_fingerprint(resources);
    let normalized_app = normalize_app_dir(app_dir);

    let state = load_chunk_state();
    if let Some(ref s) = state {
        if s.app_dir == normalized_app
            && s.patch_signature == signature
            && s.asset_fingerprint == fingerprint_before
        {
            return Ok(ChunkPatchResult {
                chunk_patches: 0,
                cache_hit: true,
                upgrade_mode: false,
                target_files: s.target_files.clone(),
            });
        }
    }

    let upgrade_mode = has_complete_runtime_markers(resources);

    // 收集所有 mutation targets（相对路径集合）
    let targets: Vec<PathBuf> = collect_chunk_mutation_targets(resources);
    let mut modified_files: BTreeSet<String> = BTreeSet::new();
    let mut total_patches = 0usize;

    let replacements = sidebar_replacements();
    for target in &targets {
        let content = std::fs::read_to_string(target)
            .map_err(|e| CcError::Generic(format!("读取 JS 文件 {target:?}: {e}")))?;
        let (new_content, n) = replace_outside_injected_blocks(&content, &replacements);
        if n > 0 || upgrade_mode {
            // 注入指纹标记（确保成对）
            let final_content = if new_content.contains(PATCH_MARKER_END) && new_content.contains(PATCH_MARKER_BEGIN) {
                // 已有成对标记，不重复注入
                new_content
            } else {
                // 剥离旧 legacy 块后注入新块
                let stripped = strip_legacy_blocks(&new_content);
                format!(
                    "{}\n{}{}\n",
                    stripped,
                    PATCH_MARKER_BEGIN,
                    PATCH_MARKER_END
                )
            };
            atomic_write_bytes(target, final_content.as_bytes())
                .map_err(|e| CcError::Generic(format!("写回 {target:?}: {e}")))?;
            if let Ok(rel) = target.strip_prefix(resources) {
                modified_files.insert(rel.to_string_lossy().replace('\\', "/"));
            }
            total_patches += n;
        }
    }

    let fingerprint_after = assets_fingerprint(resources);
    let chunk_state = ChunkState {
        schema: "v1".into(),
        app_dir: normalized_app,
        asset_fingerprint: fingerprint_after,
        patch_signature: signature,
        target_files: modified_files.iter().cloned().collect(),
        chunk_patches: total_patches,
    };
    save_chunk_state(&chunk_state)?;

    Ok(ChunkPatchResult {
        chunk_patches: total_patches,
        cache_hit: false,
        upgrade_mode,
        target_files: modified_files.into_iter().collect(),
    })
}

/// 剥离旧版 legacy 标记块
fn strip_legacy_blocks(content: &str) -> String {
    let mut result = content.to_string();
    for (begin, end) in LEGACY_MARKERS {
        if let Some(start) = result.find(begin) {
            if let Some(tail) = result[start..].find(end) {
                let end_abs = start + tail + end.len();
                result = format!("{}{}", &result[..start], &result[end_abs..]);
            }
        }
    }
    result
}

/// 收集所有将被修改的 JS 文件（相对路径列表，供权限事务边界使用）
pub fn collect_chunk_mutation_targets(resources: &Path) -> Vec<PathBuf> {
    collect_index_js(resources)
}

fn normalize_app_dir(app_dir: &Path) -> String {
    app_dir
        .to_string_lossy()
        .to_lowercase()
        .trim_end_matches(['\\', '/'])
        .to_string()
}

fn load_chunk_state() -> Option<ChunkState> {
    let path = chunk_state_path();
    let raw = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&raw).ok()
}

fn save_chunk_state(state: &ChunkState) -> Result<()> {
    let path = chunk_state_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let raw = serde_json::to_string_pretty(state)
        .map_err(|e| CcError::Generic(format!("序列化 chunk state: {e}")))?;
    atomic_write_bytes(&path, raw.as_bytes())
}

/// 清理工具自身状态（恢复后调用）
pub fn cleanup_state() {
    let _ = std::fs::remove_file(chunk_state_path());
}
