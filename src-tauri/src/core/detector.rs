// detector.rs — 定位 Claude Desktop 安装目录、杀进程、三态状态检测
// 对应 Python 版 core/detector.py
use crate::core::proc::hidden_command;
use crate::core::errors::{CcError, Result};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

/// Claude 用户配置文件（locale 等）默认路径
fn claude_config_path() -> PathBuf {
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    PathBuf::from(local).join("Claude-3p").join("config.json")
}

/// 已安装 Claude 的 app 根目录（含 resources/en-US.json）
pub fn resolve_app_dir(target: Option<&str>) -> Result<PathBuf> {
    // 1. 手动指定
    if let Some(dir) = target {
        let p = PathBuf::from(dir);
        if p.join("resources/en-US.json").exists() {
            return Ok(p);
        }
        if p.join("app/resources/en-US.json").exists() {
            return Ok(p.join("app"));
        }
        return Err(CcError::Generic(format!("手动路径 {dir} 无效")));
    }

    // 2. 运行中 claude 进程（优先级最高）
    if let Some(dir) = find_running_claude_dir() {
        return Ok(dir);
    }

    // 3. WindowsApps (Store)
    if let Some(dir) = find_windowsapps_package() {
        return Ok(dir);
    }

    // 4. LOCALAPPDATA
    if let Some(dir) = find_appdata_package() {
        return Ok(dir);
    }

    Err(CcError::NotFound)
}

/// 查运行中 claude 进程的 exe 目录
fn find_running_claude_dir() -> Option<PathBuf> {
    let output = hidden_command("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "$p = Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1; if ($p) { Split-Path -Parent $p.Path }",
        ])
        .output()
        .ok()?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let parent = PathBuf::from(line);
        if parent.join("resources/en-US.json").exists() {
            return Some(parent);
        }
        if parent.join("app/resources/en-US.json").exists() {
            return Some(parent.join("app"));
        }
    }
    None
}

/// 查 WindowsApps Store 包目录（用 PowerShell Get-AppxPackage）
fn find_windowsapps_package() -> Option<PathBuf> {
    let output = hidden_command("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Get-AppxPackage | Where-Object { $_.Name -like '*Claude*' } | Select-Object -ExpandProperty InstallLocation",
        ])
        .output()
        .ok()?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let p = PathBuf::from(line);
        // WindowsApps 里可能有 app/ 子目录
        if p.join("resources/en-US.json").exists() {
            return Some(p);
        }
        if p.join("app/resources/en-US.json").exists() {
            return Some(p.join("app"));
        }
    }
    None
}

/// 查 LOCALAPPDATA / APPDATA 下已安装的 Claude Desktop
fn find_appdata_package() -> Option<PathBuf> {
    let bases = [
        std::env::var("LOCALAPPDATA").ok(),
        std::env::var("APPDATA").ok(),
    ];
    for base in bases.iter().flatten() {
        let anthropic = PathBuf::from(base).join("AnthropicClaude");
        if !anthropic.exists() {
            continue;
        }
        // 找最新版本目录
        let versions: Vec<_> = std::fs::read_dir(&anthropic)
            .ok()?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        // 按版本号排序（简单按目录名字典序）
        for v in versions.iter().rev() {
            let app = v.join("app");
            if app.join("resources/en-US.json").exists() {
                return Some(app);
            }
            if v.join("resources/en-US.json").exists() {
                return Some(v.clone());
            }
        }
    }
    None
}

/// 杀掉所有 claude 进程，轮询确认列表为空（默认 12s 超时）
pub fn stop_claude(timeout_secs: u64) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);

    loop {
        // 尝试优雅停止
        let _ = hidden_command("powershell")
            .args(["-NoProfile", "-Command", "Stop-Process -Name claude -Force -ErrorAction SilentlyContinue"])
            .output();
        std::thread::sleep(Duration::from_secs(1));

        let alive = count_claude_processes();
        if alive == 0 {
            return Ok(());
        }
        if Instant::now() >= deadline {
            // 兜底：taskkill /F /T
            let _ = hidden_command("taskkill")
                .args(["/IM", "Claude.exe", "/F", "/T"])
                .output();
            std::thread::sleep(Duration::from_secs(2));
            return Err(CcError::ClaudeProcessAlive);
        }
    }
}

fn count_claude_processes() -> u32 {
    let stdout = match hidden_command("powershell")
        .args(["-NoProfile", "-Command", "(Get-Process -Name claude -ErrorAction SilentlyContinue).Count"])
        .output()
    {
        Ok(o) => String::from_utf8_lossy(&o.stdout).to_string(),
        Err(_) => return 0,
    };
    stdout.trim().parse().unwrap_or(0)
}

/// 启动 Claude Desktop
pub fn open_claude(app_dir: &Path) -> Result<()> {
    let exe = app_dir.join("Claude.exe");
    let status = Command::new(&exe).spawn();
    match status {
        Ok(_) => Ok(()),
        Err(e) => {
            // Claude 可能是 Store 包，直接 open shell:AppsFolder
            let _ = hidden_command("cmd").args(["/C", "start", "shell:AppsFolder"]).status();
            Err(CcError::Generic(format!("无法启动 Claude: {e}")))
        }
    }
}

/// 三态检测，对应 Python 版 build_status
pub fn build_status(target: Option<&str>) -> DetectStatus {
    let resolved = resolve_app_dir(target).ok();
    let app_dir_str = resolved.as_ref().map(|p| p.to_string_lossy().to_string());
    let is_windowsapps = app_dir_str
        .as_deref()
        .map(|s| s.contains("WindowsApps"))
        .unwrap_or(false);

    let status = match resolved {
        None => DetectStatus {
            state: "missing".into(),
            message: "未找到 Claude Desktop 安装。".into(),
            version: None,
            is_windowsapps: false,
            app_dir: None,
        },
        Some(dir) => {
            let version = read_version(&dir);
            let localized = is_localized(&dir);
            DetectStatus {
                state: if localized { "ready" } else { "repair" }.into(),
                message: if localized {
                    "Claude Desktop 中文版可以打开。".into()
                } else {
                    "Claude Desktop 已找到，可以安装中文补丁。".into()
                },
                version: version.clone(),
                is_windowsapps,
                app_dir: Some(dir.to_string_lossy().to_string()),
            }
        }
    };

    status
}

/// 读 Claude 版本号（从 package.json 或 exe 文件元数据）
fn read_version(app_dir: &Path) -> Option<String> {
    let pkg = app_dir.join("package.json");
    if pkg.exists() {
        let content = std::fs::read_to_string(&pkg).ok()?;
        let v: serde_json::Value = serde_json::from_str(&content).ok()?;
        return v.get("version")?.as_str().map(|s| s.to_string());
    }
    None
}

/// 检查当前安装是否已被汉化（zh 资源存在 + locale 已设）
fn is_localized(app_dir: &Path) -> bool {
    // 检查 chunk patch marker 存在
    let markers = [
        "__CLAUDE_ZH_CN_PATCH_BEGIN__",
        "__CLAUDE_ZH_CN_FONT_PATCH_BEGIN__",
    ];
    if let Ok(entries) = std::fs::read_dir(app_dir.join("resources")) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().map(|e| e == "js").unwrap_or(false) {
                if let Ok(content) = std::fs::read(&path) {
                    // 只检查文件尾部 4 MB（marker 注入在尾部附近）
                    let len = content.len();
                    let start = len.saturating_sub(4 * 1024 * 1024);
                    let tail = &content[start..];
                    for m in &markers {
                        if tail.windows(m.len()).any(|w| w == m.as_bytes()) {
                            return true;
                        }
                    }
                }
            }
        }
    }
    // 也检查 locale
    let config = claude_config_path();
    if let Ok(content) = std::fs::read_to_string(&config) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&content) {
            if v.get("locale").and_then(|l| l.as_str()) == Some("zh-CN") {
                return true;
            }
        }
    }
    false
}

#[derive(Serialize, Clone, Debug)]
pub struct DetectStatus {
    pub state: String,
    pub message: String,
    pub version: Option<String>,
    pub is_windowsapps: bool,
    pub app_dir: Option<String>,
}
