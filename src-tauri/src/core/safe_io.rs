// safe_io.rs — 原子写原语 + SHA-256
use crate::core::errors::{CcError, Result};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::Path;

/// 计算文件 SHA-256，分块读入，不整体载入内存。
pub fn sha256_file(path: &Path) -> Result<[u8; 32]> {
    let mut f = fs::File::open(path)
        .map_err(|e| CcError::Generic(format!("无法打开 {path:?}: {e}")))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = match f.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) => {
                return Err(CcError::Generic(format!("读取 {path:?}: {e}")));
            }
        };
        hasher.update(&buf[..n]);
    }
    let digest = hasher.finalize();
    let mut out = [0u8; 32];
    out.copy_from_slice(&digest[..32]);
    Ok(out)
}

/// 计算字节序列 SHA-256（十六进制小写）。
pub fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    let digest = hasher.finalize();
    let mut s = String::with_capacity(64);
    for b in &digest {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// 原子写文件：同目录临时文件 + flush + fsync + rename。
/// 进程被杀或断电时目标文件要么是原内容、要么是新内容，绝不半截。
///
/// 临时文件名为 `.原名.cc-zh-tmp.<pid>`，位于目标文件同一目录，
/// 确保跨卷 rename 不会发生（同一文件系统内 rename 才原子）。
pub fn atomic_write_bytes(path: &Path, data: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));

    if !parent.exists() {
        return Err(CcError::Generic(format!("父目录不存在: {parent:?}")));
    }

    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| CcError::Generic("文件名不可读".into()))?;
    let pid = std::process::id();
    let tmp_path = parent.join(format!(".{file_name}.cc-zh-tmp.{pid}"));

    // 写临时文件，guard 确保异常时清理
    struct Guard(std::path::PathBuf);
    impl Drop for Guard {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }
    let guard = Guard(tmp_path.clone());

    {
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp_path)
            .map_err(|e| CcError::Generic(format!("无法创建临时文件 {tmp_path:?}: {e}")))?;
        f.write_all(data)
            .map_err(|e| CcError::Generic(format!("写入临时文件 {tmp_path:?}: {e}")))?;
        f.flush()
            .map_err(|e| CcError::Generic(format!("刷新临时文件 {tmp_path:?}: {e}")))?;
        f.sync_data()
            .map_err(|e| CcError::Generic(format!("fsync 临时文件 {tmp_path:?}: {e}")))?;
    }

    // rename 原子替换目标；成功后取消 guard 的自动清理
    let result = fs::rename(&tmp_path, path);
    drop(guard);
    result.map(|_| ()).map_err(|e| CcError::Generic(format!("rename {tmp_path:?} → {path:?}: {e}")))
}

/// 原子写文本（UTF-8）便捷包装。
pub fn atomic_write_text(path: &Path, text: &str) -> Result<()> {
    atomic_write_bytes(path, text.as_bytes())
}
