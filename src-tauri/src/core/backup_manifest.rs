// backup_manifest.rs — 快照 manifest 状态机 + install_id 版本绑定 + 哈希校验
// 对应 Python 版 core/backup_manifest.py
//
// 状态机不变量：staging → (seal) → sealed → (commit) → complete / invalid
// seal 之后 files 列表锁定，不可追加或修改。
use crate::core::safe_io::{atomic_write_text, sha256_file};
use crate::core::errors::{CcError, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// 快照状态
#[derive(Serialize, Deserialize, Clone, PartialEq, Debug)]
#[serde(rename_all = "lowercase")]
pub enum Phase {
    Staging,
    Sealed,
    Complete,
    Invalid,
}

/// manifest.json 结构
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Manifest {
    pub status: Phase,
    pub install_id: String,
    pub app_dir: String,
    pub version: String,
    pub created_at: String,
    /// 相对路径（相对于 resources）→ 文件记录
    pub files: HashMap<String, FileRecord>,
    /// 用户配置原值（locale / claudeZhCnFont）
    pub user_config: Option<UserConfigOriginal>,
    /// 无效化原因（status == Invalid 时）
    pub invalid_reason: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct FileRecord {
    /// 该文件在基线中是否原本存在
    pub existed: bool,
    /// 基线时（existed=true）或补丁后（existed=false）的 SHA-256
    pub original_sha256: Option<String>,
    /// 打补丁后的 SHA-256（commit 时填充）
    pub patched_sha256: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct UserConfigOriginal {
    pub locale: Option<String>,
    pub font: Option<String>,
}

/// 哈希校验结果
#[derive(Clone, Debug)]
pub struct Verification {
    pub valid: bool,
    pub reasons: Vec<String>,
}

const SNAPSHOT_SUBDIR: &str = "Claude-zh-CN-official-backup";

/// 快照根目录：WindowsApps 提权场景走 ProgramData，普通场景走 LocalAppData
pub fn snapshot_root(protected: bool) -> PathBuf {
    if protected {
        let programdata = std::env::var("PROGRAMDATA")
            .or_else(|_| std::env::var("ProgramData"))
            .unwrap_or_else(|_| r"C:\ProgramData".into());
        PathBuf::from(programdata).join(SNAPSHOT_SUBDIR).join("snapshots")
    } else {
        let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
        PathBuf::from(local).join(SNAPSHOT_SUBDIR).join("snapshots")
    }
}

/// 计算 install_id：app 目录 + 稳定文件 SHA-256 + 资源布局 → 32 hex 指纹
pub fn build_install_identity(app_dir: &Path, version: Option<&str>) -> String {
    let resources = app_dir.join("resources");
    let layout = js_layout(&resources);

    // 稳定文件：优先 app.asar，其次第一个 .exe，最后 en-US.json
    let stable = if resources.join("app.asar").is_file() {
        resources.join("app.asar")
    } else if let Ok(entries) = std::fs::read_dir(app_dir) {
        entries
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.extension().map(|e| e == "exe").unwrap_or(false))
            .min_by(|a, b| a.file_name().cmp(&b.file_name()))
            .unwrap_or_else(|| resources.join("en-US.json"))
    } else {
        resources.join("en-US.json")
    };

    let stable_hash = sha256_file(&stable).unwrap_or_default();
    let version = version.map(|s| s.to_string()).unwrap_or_else(|| "unknown".into());

    // 简单指纹：SHA-256(app_dir + version + layout + stable_hash) 取前 32 hex
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(app_dir.to_string_lossy().as_bytes());
    hasher.update(b"\0");
    hasher.update(version.as_bytes());
    hasher.update(b"\0");
    for item in &layout {
        hasher.update(item.as_bytes());
        hasher.update(b"\n");
    }
    hasher.update(b"\0");
    hasher.update(&stable_hash);
    let digest = hasher.finalize();
    let mut s = String::with_capacity(32);
    for b in &digest[..16] {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// 列出 resources 下 JS 资源布局（用于身份指纹）
fn js_layout(resources: &Path) -> Vec<String> {
    let assets = resources.join("ion-dist/assets");
    let mut out = Vec::new();
    walk_js(&assets, &mut out);
    out.sort();
    out
}

fn walk_js(dir: &Path, out: &mut Vec<String>) {
    if let Ok(entries) = std::fs::read_dir(dir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                walk_js(&p, out);
            } else if p.extension().map(|e| e == "js").unwrap_or(false) {
                // assets 的祖父是 resources，用 resources 作为前缀取相对路径
                if let Some(resources) = dir.parent().and_then(|x| x.parent()) {
                    if let Ok(rel) = p.strip_prefix(resources) {
                        out.push(rel.to_string_lossy().replace('\\', "/"));
                    }
                }
            }
        }
    }
}

/// 创建密封快照（staging → 逐文件 stage → seal）
pub fn create_sealed_snapshot(
    app_dir: &Path,
    targets: &[String], // 相对 resources 的路径列表
    protected: bool,
) -> Result<(PathBuf, Manifest)> {
    let root = snapshot_root(protected);
    std::fs::create_dir_all(&root).map_err(|e| CcError::Generic(format!("创建快照根 {root:?}: {e}")))?;

    let version = infer_version(app_dir);
    let id = build_install_identity(app_dir, Some(&version));
    let snapshot_dir = root.join(&id);

    if snapshot_dir.exists() {
        // 已有快照：加载并校验
        let manifest_path = snapshot_dir.join("manifest.json");
        if manifest_path.is_file() {
            let raw = std::fs::read_to_string(&manifest_path)
                .map_err(|e| CcError::Generic(format!("读取 manifest {manifest_path:?}: {e}")))?;
            let manifest: Manifest = serde_json::from_str(&raw)
                .map_err(|e| CcError::Generic(format!("解析 manifest: {e}")))?;
            let existing_targets: Vec<String> = manifest.files.keys().cloned().collect();
            if existing_targets != targets.to_vec() {
                return Err(CcError::SnapshotMismatch);
            }
            let verification = verify_payload(&snapshot_dir, &manifest)?;
            if !verification.valid {
                return Err(CcError::Generic(format!(
                    "现有快照哈希校验失败: {:?}",
                    verification.reasons
                )));
            }
            return Ok((snapshot_dir, manifest));
        }
        // 目录存在但没有 manifest.json = 上次崩溃残留，fail-closed 删除
        discard_incomplete_snapshot(&snapshot_dir, &root)?;
    }

    std::fs::create_dir_all(&snapshot_dir)
        .map_err(|e| CcError::Generic(format!("创建快照目录 {snapshot_dir:?}: {e}")))?;

    let mut manifest = Manifest {
        status: Phase::Staging,
        install_id: id.clone(),
        app_dir: app_dir.to_string_lossy().to_string(),
        version,
        created_at: utc_now(),
        files: HashMap::new(),
        user_config: None,
        invalid_reason: None,
    };

    // 逐文件 stage
    for rel in targets {
        stage_resource_file(&mut manifest, &snapshot_dir, app_dir, rel)?;
    }

    // seal
    manifest.status = Phase::Sealed;
    write_manifest(&snapshot_dir, &manifest)?;

    Ok((snapshot_dir, manifest))
}

/// 把单个资源文件存入快照 payload
fn stage_resource_file(
    manifest: &mut Manifest,
    snapshot_dir: &Path,
    app_dir: &Path,
    rel: &str,
) -> Result<()> {
    let resources = app_dir.join("resources");
    let source = resources.join(rel);

    let existed = source.is_file();
    let payload_rel = payload_rel(rel);
    let payload_path = snapshot_dir.join(&payload_rel);
    std::fs::create_dir_all(payload_path.parent().unwrap_or(&payload_path))
        .map_err(|e| CcError::Generic(format!("创建 payload 目录 {payload_path:?}: {e}")))?;

    let original_hash = if existed {
        let hash = sha256_file(&source)?;
        // 原子拷贝 payload
        let bytes = std::fs::read(&source)
            .map_err(|e| CcError::Generic(format!("读取 {source:?}: {e}")))?;
        crate::core::safe_io::atomic_write_bytes(&payload_path, &bytes)?;
        // 复核：写入后 hash 必须一致
        let written = sha256_file(&payload_path)?;
        if written != hash {
            return Err(CcError::Generic(format!(
                "payload 哈希复核失败 {rel}: 读 {hash:?} 写 {written:?}"
            )));
        }
        Some(hex_string(&hash))
    } else {
        None
    };

    manifest.files.insert(
        rel.to_string(),
        FileRecord {
            existed,
            original_sha256: original_hash,
            patched_sha256: None,
        },
    );
    Ok(())
}

/// 记录补丁后文件哈希（commit 阶段调用）
pub fn record_patched_file(manifest: &mut Manifest, rel: &str, patched_sha256_hex: &str) -> Result<()> {
    if manifest.status != Phase::Sealed {
        return Err(CcError::Generic(format!(
            "manifest 状态 {:?} 不是 Sealed，不能记录 patched 哈希",
            manifest.status
        )));
    }
    let record = manifest
        .files
        .get_mut(rel)
        .ok_or_else(|| CcError::Generic(format!("文件 {rel} 不在 manifest 中")))?;
    record.patched_sha256 = Some(patched_sha256_hex.to_string());
    Ok(())
}

/// 提交 manifest → complete
pub fn commit_manifest(snapshot_dir: &Path, manifest: &mut Manifest) -> Result<()> {
    if manifest.status != Phase::Sealed {
        return Err(CcError::Generic("commit 要求状态为 Sealed".into()));
    }
    let verification = verify_payload(snapshot_dir, manifest)?;
    if !verification.valid {
        return Err(CcError::Generic(format!(
            "commit 前 payload 校验失败: {:?}",
            verification.reasons
        )));
    }
    for (rel, record) in manifest.files.iter() {
        if record.existed && record.patched_sha256.is_none() {
            return Err(CcError::Generic(format!(
                "文件 {rel} 缺少 patched_sha256，不能提交"
            )));
        }
    }
    manifest.status = Phase::Complete;
    write_manifest(snapshot_dir, manifest)
}

/// 逐文件复核 payload 哈希
pub fn verify_payload(snapshot_dir: &Path, manifest: &Manifest) -> Result<Verification> {
    let mut reasons = Vec::new();
    for (rel, record) in manifest.files.iter() {
        if !record.existed {
            continue;
        }
        let payload = snapshot_dir.join(payload_rel(rel));
        if !payload.is_file() {
            reasons.push(format!("payload 缺失: {rel}"));
            continue;
        }
        let hash = match sha256_file(&payload) {
            Ok(h) => h,
            Err(e) => {
                reasons.push(format!("payload 读取失败 {rel}: {e}"));
                continue;
            }
        };
        let expected = hex_string(&hash);
        if let Some(orig) = &record.original_sha256 {
            if *orig != expected {
                reasons.push(format!("哈希不符 {rel}"));
            }
        }
    }
    Ok(Verification {
        valid: reasons.is_empty(),
        reasons,
    })
}

/// 计算快照目录路径（install_id 决定）
pub fn snapshot_dir_for(app_dir: &Path, protected: bool) -> PathBuf {
    let root = snapshot_root(protected);
    let id = build_install_identity(app_dir, None);
    root.join(&id)
}

/// 加载已有快照 manifest（install_id 匹配时返回 Some，不存在返回 None）
pub fn load_existing_snapshot(
    app_dir: &Path,
    protected: bool,
) -> Result<Option<Manifest>> {
    let dir = snapshot_dir_for(app_dir, protected);
    let manifest_path = dir.join("manifest.json");
    if !manifest_path.is_file() {
        return Ok(None);
    }
    let raw = std::fs::read_to_string(&manifest_path)
        .map_err(|e| CcError::Generic(format!("读取 manifest {manifest_path:?}: {e}")))?;
    let manifest: Manifest =
        serde_json::from_str(&raw).map_err(|e| CcError::Generic(format!("解析 manifest: {e}")))?;
    Ok(Some(manifest))
}

/// payload 目录名（相对快照根）
pub fn payload_rel_for(rel: &str) -> String {
    payload_rel(rel)
}

/// 加载与当前安装身份严格匹配的 manifest（恢复前校验）
pub fn load_matching_manifest(
    app_dir: &Path,
    protected: bool,
) -> Result<(PathBuf, Manifest)> {
    let root = snapshot_root(protected);
    let id = build_install_identity(app_dir, None);
    let snapshot_dir = root.join(&id);
    let manifest_path = snapshot_dir.join("manifest.json");
    if !manifest_path.is_file() {
        return Err(CcError::Generic(format!(
            "未找到与当前安装匹配的快照 (install_id={id})"
        )));
    }
    let raw = std::fs::read_to_string(&manifest_path)
        .map_err(|e| CcError::Generic(format!("读取 manifest: {e}")))?;
    let manifest: Manifest = serde_json::from_str(&raw)
        .map_err(|e| CcError::Generic(format!("解析 manifest: {e}")))?;
    if manifest.install_id != id {
        return Err(CcError::SnapshotMismatch);
    }
    if manifest.status != Phase::Complete {
        return Err(CcError::Generic(format!(
            "快照状态 {:?} 不是 Complete，不能用于恢复",
            manifest.status
        )));
    }
    let verification = verify_payload(&snapshot_dir, &manifest)?;
    if !verification.valid {
        return Err(CcError::Generic(format!(
            "快照哈希校验失败: {:?}",
            verification.reasons
        )));
    }
    Ok((snapshot_dir, manifest))
}

/// fail-closed：删除不完整快照（无 manifest.json 的残留目录）
pub fn discard_incomplete_snapshot(snapshot: &Path, expected_root: &Path) -> Result<()> {
    // 安全边界：只允许删除快照根直接子目录，且名字必须合法 install_id 格式
    let name = snapshot.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if !is_valid_install_id(name) {
        return Ok(()); // 不是我们的目录，不碰
    }
    if let Some(parent) = snapshot.parent() {
        if parent.canonicalize().ok() != expected_root.canonicalize().ok() {
            return Ok(());
        }
    }
    let manifest_path = snapshot.join("manifest.json");
    if manifest_path.is_file() {
        return Ok(()); // 有 manifest，不是"不完整"，不动
    }
    std::fs::remove_dir_all(snapshot)
        .map_err(|e| CcError::Generic(format!("删除不完整快照 {snapshot:?}: {e}")))?;
    Ok(())
}

/// 判断目录名是否为合法 install_id（32 位十六进制）
fn is_valid_install_id(name: &str) -> bool {
    name.len() == 32 && name.chars().all(|c| c.is_ascii_hexdigit())
}

/// 推断 Claude 版本号
fn infer_version(app_dir: &Path) -> String {
    let candidates = [
        app_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()),
        app_dir
            .parent()
            .and_then(|p| p.file_name().and_then(|n| n.to_str()))
            .map(|s| s.to_string()),
    ];
    for name in candidates.iter().flatten() {
        // 简单正则匹配 Claude_1.40609.0.0 或类似格式
        if let Some(cap) = name.find(|c: char| c.is_ascii_digit()) {
            let digits = &name[cap..];
            let end = digits.find(|c: char| !c.is_ascii_digit() && c != '.').unwrap_or(digits.len());
            if end >= 4 {
                return digits[..end].to_string();
            }
        }
    }
    "unknown".into()
}

fn write_manifest(snapshot_dir: &Path, manifest: &Manifest) -> Result<()> {
    let raw = serde_json::to_string_pretty(manifest)
        .map_err(|e| CcError::Generic(format!("序列化 manifest: {e}")))?;
    atomic_write_text(&snapshot_dir.join("manifest.json"), &raw)
}

fn payload_rel(rel: &str) -> String {
    format!("payload/{rel}")
}

fn hex_string(bytes: &[u8; 32]) -> String {
    let mut s = String::with_capacity(64);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

fn utc_now() -> String {
    use std::time::SystemTime;
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (y, m, d) = days_to_ymd(secs / 86400);
    format!("{y:04}-{m:02}-{d:02}T00:00:00Z")
}

/// 简单格里高利历转换（足够用于快照时间戳，不需完整日期库）
fn days_to_ymd(mut days: u64) -> (u32, u32, u32) {
    let mut year = 1970u32;
    loop {
        let is_leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
        let year_days = if is_leap { 366 } else { 365 } as u64;
        if days < year_days {
            break;
        }
        days -= year_days;
        year += 1;
    }
    let is_leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let month_days: [u64; 12] = [
        31,
        if is_leap { 29 } else { 28 },
        31, 30, 31, 30, 31, 31, 30, 31, 30, 31,
    ];
    let mut month = 1u32;
    while month <= 12 && days >= month_days[month as usize - 1] {
        days -= month_days[month as usize - 1];
        month += 1;
    }
    (year, month, (days + 1) as u32)
}
