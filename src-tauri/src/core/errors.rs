// errors.rs — 统一错误类型 + 归一化错误码表
use thiserror::Error;

#[derive(Error, Debug)]
pub enum CcError {
    #[error("{0}")]
    Generic(String),

    #[error("PERMISSION_DENIED: 普通权限无法写入 WindowsApps 目录，请以管理员运行")]
    PermissionDenied,

    #[error("SNAPSHOT_MISMATCH: 现有快照与当前补丁目标计划不一致")]
    SnapshotMismatch,

    #[error("BASELINE_DIRTY: 基线已被第三方修改，请先使用官方修复/重装 Claude")]
    BaselineDirty,

    #[error("VERSION_UNSUPPORTED: Claude 版本布局未适配，拒绝写入")]
    VersionUnsupported,

    #[error("VERSION_UNKNOWN: 无法识别的 Claude 资源布局")]
    VersionUnknown,

    #[error("ELEVATION_TAMPERED: 提权结果文件缺失或格式错误")]
    ElevationTampered,

    #[error("CLAUDE_PROCESS_ALIVE: Claude 进程仍占用资源文件")]
    ClaudeProcessAlive,

    #[error("NOT_FOUND: 未找到 Claude Desktop 安装")]
    NotFound,

    #[error("IO_ERROR: {0}")]
    Io(#[from] std::io::Error),

    #[error("JSON_ERROR: {0}")]
    Json(#[from] serde_json::Error),
}

impl CcError {
    pub fn code(&self) -> &'static str {
        match self {
            CcError::PermissionDenied => "PERMISSION_DENIED",
            CcError::SnapshotMismatch => "SNAPSHOT_MISMATCH",
            CcError::BaselineDirty => "BASELINE_DIRTY",
            CcError::VersionUnsupported => "VERSION_UNSUPPORTED",
            CcError::VersionUnknown => "VERSION_UNKNOWN",
            CcError::ElevationTampered => "ELEVATION_TAMPERED",
            CcError::ClaudeProcessAlive => "CLAUDE_PROCESS_ALIVE",
            CcError::NotFound => "NOT_FOUND",
            CcError::Generic(_) => "GENERIC_ERROR",
            CcError::Io(_) => "IO_ERROR",
            CcError::Json(_) => "JSON_ERROR",
        }
    }
}

pub type Result<T> = std::result::Result<T, CcError>;
