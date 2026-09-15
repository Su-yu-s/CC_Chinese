// lib.rs — Tauri 插件 + 命令注册
pub mod core;
mod commands;

pub fn run() -> tauri::Result<()> {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            commands::detect,
            commands::install,
            commands::restore,
            commands::open_claude,
        ])
        .run(tauri::generate_context!())
}
