// commands.rs — Tauri 命令薄封装
use tauri;
use crate::core;

/// 检测 Claude 安装状态
#[tauri::command]
pub fn detect(target: Option<String>) -> core::detector::DetectStatus {
    core::detector::build_status(target.as_deref())
}

/// 执行汉化
#[tauri::command]
pub fn install(app_dir: String, elevated: bool) -> core::installer::OpResult {
    core::installer::run_install(&app_dir, elevated)
}

/// 恢复官方原状
#[tauri::command]
pub fn restore(app_dir: String, elevated: bool) -> core::installer::OpResult {
    core::installer::run_restore(&app_dir, elevated)
}

/// 启动 Claude Desktop
#[tauri::command]
pub fn open_claude(app_dir: String) -> core::installer::OpResult {
    core::installer::run_open(&app_dir)
}
