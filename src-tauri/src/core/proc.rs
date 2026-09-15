// proc.rs — 子进程统一走这里：GUI 程序起控制台子进程必须带 CREATE_NO_WINDOW，
// 否则 Windows 会为每次 powershell/icacls 调用闪一个黑窗
// （本项目按 spec 仅支持 Windows，不做跨平台 cfg）

use std::os::windows::process::CommandExt;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 创建不弹控制台窗口的子进程 Command
pub fn hidden_command(program: &str) -> std::process::Command {
    let mut cmd = std::process::Command::new(program);
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}
