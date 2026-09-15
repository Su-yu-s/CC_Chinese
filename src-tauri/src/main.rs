// main.rs — 入口：检测 --elevated-action 提权子进程分支，否则初始化 Tauri GUI
// release 构建隐藏控制台窗口（GUI 子系统）；debug 保留控制台便于看日志
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;

fn main() {
    // 提权子进程分支：跳过 Tauri 初始化，直接走 core::elevation
    let args: Vec<String> = env::args().collect();
    if args.iter().any(|a| a == "--elevated-action") {
        let get_arg = |flag: &str| -> String {
            args.iter()
                .position(|a| a == flag)
                .and_then(|i| args.get(i + 1).cloned())
                .unwrap_or_default()
        };
        let action = get_arg("--elevated-action");
        let target = get_arg("--target-hint");
        let nonce = get_arg("--nonce");
        std::process::exit(cc_chinese_lib::core::elevation::run_elevated_cli(
            &action, &target, &nonce,
        ));
    }

    // 正常 Tauri GUI 分支
    cc_chinese_lib::run().expect("error while running tauri application");
}
