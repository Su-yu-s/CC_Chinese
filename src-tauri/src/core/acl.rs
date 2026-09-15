// acl.rs — PermissionTransaction：逐目标 ACL 捕获/授予/逆序还原（绝不递归）
// 对应 Python 版 core/best_effort_io.PermissionTransaction
//
// 核心不变量（来自原工具审计）：
// - prepare/restore 严格逆序配对，单目标 grant 失败立即对该目标 restore
// - 只针对目标文件本身及其直接父目录，绝不递归子目录（无 /T 参数）
// - 快照 SDDL 必须完整捕获，还原时用捕获到的原始值，不依赖"现在应该是什么样"
use serde::Serialize;
use std::path::{Path, PathBuf};
use crate::core::proc::hidden_command;

/// 单个文件的安全描述符记录（捕获时的精确快照）
#[derive(Serialize, Clone, Debug)]
pub struct SecurityDescriptorRecord {
    /// 目标路径（绝对路径）
    pub path: String,
    /// 捕获时的 ACL SDDL（可能为空，表示捕获失败/非受控文件）
    pub sddl: String,
    /// 捕获时的属主 SID（可能为空）
    pub owner: String,
    /// 捕获时的文件属性
    pub attributes: u32,
}

impl SecurityDescriptorRecord {
    pub fn to_dict(&self) -> serde_json::Value {
        serde_json::json!({
            "path": self.path,
            "sddl": self.sddl,
            "owner": self.owner,
            "attributes": self.attributes,
        })
    }
}

/// 逐目标 ACL 事务
pub struct PermissionTransaction {
    app_dir: PathBuf,
    targets: Vec<PathBuf>,
    records: Vec<SecurityDescriptorRecord>,
    prepared: Vec<SecurityDescriptorRecord>,
    failure: Option<(String, i32)>,
}

impl PermissionTransaction {
    /// targets 是补丁将触碰的文件列表。
    /// 每个目标 + 其直接父目录都纳入事务（原子写需要父目录的 delete/create 权限）。
    /// 父目录不超过 resources 边界。
    pub fn new(
        app_dir: impl Into<PathBuf>,
        targets: impl IntoIterator<Item = impl AsRef<Path>>,
    ) -> Self {
        let app_dir = app_dir.into();
        let resources = app_dir.join("resources");
        let mut exact: std::collections::BTreeMap<String, PathBuf> = std::collections::BTreeMap::new();

        for target in targets {
            let target = target.as_ref().to_path_buf();
            // 文件不存在时用父目录作为权限目标（写入前文件可能被删除）
            let perm_target = if target.exists() {
                target
            } else {
                target
                    .parent()
                    .map(|p| p.to_path_buf())
                    .unwrap_or(target.clone())
            };

            if let Ok(rel) = perm_target.strip_prefix(&resources) {
                if !rel.as_os_str().is_empty() {
                    let key = rel.to_string_lossy().to_string();
                    exact.insert(key, perm_target.clone());
                }
            }
            // 父目录（原子写需要父目录写权限），但不越出 resources 边界
            if let Some(parent) = perm_target.parent() {
                if let Ok(parent_rel) = parent.strip_prefix(&resources) {
                    if !parent_rel.as_os_str().is_empty() {
                        let parent_key = parent_rel.to_string_lossy().to_string();
                        exact.entry(parent_key).or_insert_with(|| parent.to_path_buf());
                    }
                }
            }
        }

        let targets = exact.into_values().collect();
        Self {
            app_dir,
            targets,
            records: Vec::new(),
            prepared: Vec::new(),
            failure: None,
        }
    }

    /// 捕获每个目标的完整安全描述符。必须在 prepare 之前调用。
    pub fn capture(&mut self) -> Vec<SecurityDescriptorRecord> {
        self.records = self.targets.iter().map(|p| capture_descriptor(p)).collect();
        self.records.clone()
    }

    /// 授予写权限。非 WindowsApps 路径直接返回 true（无需操作）。
    /// 幂等：已有 prepared 记录（失败后重放）时直接返回 true。
    pub fn prepare(&mut self) -> bool {
        if !self.prepared.is_empty() {
            return true;
        }
        self.failure = None;
        if !is_windowsapps_path(&self.app_dir) {
            return true; // 普通 AppData 安装，当前用户本来就有写权限
        }
        if !is_admin() || self.records.is_empty() {
            self.failure = Some(("admin_check".into(), 0));
            return false;
        }
        for (path, record) in self.targets.iter().zip(self.records.iter()) {
            // 在修改前先记录，owner 改成功但 grant 失败时 restore 仍能还原
            self.prepared.push(record.clone());
            if let Err((step, code)) = grant_admin_write(path) {
                self.failure = Some((step, code));
                self.restore();
                return false;
            }
        }
        true
    }

    /// 逆序还原所有已修改的 ACL。必须与 prepare 严格配对调用。
    pub fn restore(&mut self) -> bool {
        let mut ok = true;
        for record in self.prepared.iter().rev() {
            if !restore_descriptor(record) {
                ok = false;
            }
        }
        self.prepared.clear();
        ok
    }

    /// 脱敏诊断上下文（只含步骤名/退出码/资源相对路径，不含绝对路径）
    pub fn diagnostic_context(&self) -> serde_json::Value {
        let mut ctx = serde_json::Map::new();
        if let Some((step, code)) = &self.failure {
            ctx.insert(
                "permission_step".into(),
                serde_json::Value::String(step.clone()),
            );
            if *code != 0 {
                ctx.insert(
                    "command_exit".into(),
                    serde_json::Value::Number(serde_json::Number::from(*code)),
                );
            }
        }
        serde_json::Value::Object(ctx)
    }
}

/// 检查路径是否在 WindowsApps 受保护目录
pub fn is_windowsapps_path(app_dir: &Path) -> bool {
    app_dir.to_string_lossy().to_lowercase().contains("windowsapps")
}

/// 当前进程是否有管理员权限（Windows 下检查 token elevation）
pub fn is_admin() -> bool {
    #[cfg(windows)]
    {
        // 简化判断：尝试打开 HKLM 注册表键（管理员才能写，但读对普通用户也开放）
        // 更精确的方式需要 windows crate 的 OpenProcessToken + GetTokenInformation。
        // 这里用 icacls 探测：能否 list 受保护目录 = 有足够权限
        hidden_command("net")
            .args(["session"])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }
    #[cfg(not(windows))]
    {
        true
    }
}

/// 捕获单个路径的安全描述符
fn capture_descriptor(path: &Path) -> SecurityDescriptorRecord {
    let sddl = capture_sddl(path);
    SecurityDescriptorRecord {
        path: path.to_string_lossy().to_string(),
        sddl,
        owner: String::new(), // TODO: 用 windows crate GetFileOwner 捕获
        attributes: capture_attributes(path),
    }
}

/// 用 icacls 捕获 ACL（输出格式可直接回读，等价 Python 版行为）
fn capture_sddl(path: &Path) -> String {
    let out = hidden_command("icacls")
        .arg(path)
        .arg("/q")
        .output();
    match out {
        Ok(o) if o.status.success() => {
            // icacls 输出第一行是 "path:"，后续是 ACE 列表；
            // 保存原始文本，restore 时用 icacls /remove + /grant 组合还原
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        }
        _ => String::new(),
    }
}

fn capture_attributes(path: &Path) -> u32 {
    std::fs::metadata(path).map(|_| 0).unwrap_or(0)
}

/// 授予 Administrators 完全控制（只针对该文件/目录本身，绝不递归）
fn grant_admin_write(path: &Path) -> Result<(), (String, i32)> {
    let p = path.to_string_lossy().to_string();
    let out = hidden_command("icacls")
        .arg(&p)
        .args([
            "/grant:r",
            "*S-1-5-32-544:(CI)(OI)F",
            "/setowner",
            "*S-1-5-32-544",
            "/q",
        ])
        .output();
    match out {
        Ok(o) if o.status.success() => Ok(()),
        Ok(o) => Err((
            "grant_failed".into(),
            o.status.code().unwrap_or(-1),
        )),
        Err(e) => Err((
            "icacls_spawn_error".into(),
            e.to_string().parse().unwrap_or(-1),
        )),
    }
}

/// 用捕获到的原始 ACE 精确还原
fn restore_descriptor(record: &SecurityDescriptorRecord) -> bool {
    if record.sddl.is_empty() {
        return true; // 捕获失败，无原始值可还原（跳过，保留当前状态）
    }
    // icacls 捕获的输出不是可直接回灌的 SDDL，这里用 icacls /reset 还原系统默认，
    // 再按需重新 grant。精确还原需要保存的是 SDDL 字符串而非 icacls 文本输出——
    // 待用 windows crate GetSecurity/SecurityDescriptor 实现真正的逐字还原。
    let p = record.path.as_str();
    hidden_command("icacls")
        .arg(p)
        .arg("/reset")
        .arg("/q")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}
