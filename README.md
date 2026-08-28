# CC_Chinese — Claude Desktop 中文助手

给 Windows 版 Claude Desktop 打 zh-CN 中文本地化补丁的用户级桌面工具。

---

## 项目背景

本项目基于 [Jyy1529/claude-desktop_win-zh_cn](https://github.com/Jyy1529/claude-desktop_win-zh_cn) 二次开发，
原作者为 **Jyy1529（Jash）**。

原版项目以 PowerShell 脚本 + Tauri 桌面壳的方式提供汉化补丁，功能完整但依赖较多、打包体积大。
CC_Chinese 在此基础上重新设计了用户交互层：采用 PySide6 构建独立桌面 GUI，
将补丁逻辑封装为可导入的 Python 模块，并通过 PyInstaller 打包为单个可执行文件，开箱即用。

**原项目地址：** [Jyy1529/claude-desktop_win-zh_cn](https://github.com/Jyy1529/claude-desktop_win-zh_cn)

---

## 功能

- 自动检测 WindowsApps 和 AppData 版 Claude Desktop 安装目录
- 手动选择 Claude `app` 目录（检测失败时）
- <img width="552" height="618" alt="3b40ef32196048168ffbad116f3d9111" src="https://github.com/user-attachments/assets/d7221228-d838-4fe4-87ee-d7934508a4bc" />
- 一键安装中文补丁（JSON 资源 + JS chunk 硬编码文案）
- <img width="550" height="618" alt="QQ_1787917105456" src="https://github.com/user-attachments/assets/566357a0-1018-4622-83c4-369afd352fe4" />
- 一键恢复官方英文文件
- 后台执行补丁，前台显示进度与实时日志，窗口可自由拖拽
- 单实例锁防多开，PyInstaller onedir 打包避免临时目录问题

---

## 技术栈

- **Python 3.13** + **PySide6**（Qt 官方维护，LGPL 授权，可闭源分发）
- 原生无边框窗口 + QPainter 自绘（靶心图标 / 脉冲点 / 进度条）+ QSS 皮肤
- 后台 `QThread` 运行补丁，进度通过 Signal 回主线程，不冻结界面
- PyInstaller onedir 打包（约 97MB）

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行（需 Python 3.13 + PySide6）
python main.py
```

### 打包

```bash
pip install pyinstaller
python build.py
# 产物：dist/CC_Chinese/CC_Chinese.exe
```

### 测试

```bash
python tests/test_patch_logic.py       # 汉化逻辑（临时假环境，不碰真实系统）
QT_QPA_PLATFORM=offscreen python tests/gui_smoke.py
```

---

## 项目结构

```
main.py               入口：UI + 状态机 + Worker + 单实例锁
core/
  detector.py         检测安装目录 / 版本 / 汉化状态
  patch_json.py       run_patch: 写 zh-CN 资源 + 白名单 + locale
  patch_chunks.py     run_patch_chunks: chunk 文案 + 字体/会话增强运行时
  restore.py          run_restore: 从备份还原官方文件
  installer.py        编排 run_install / run_restore / run_open / check_update
  best_effort_io.py   权限检测 + WindowsApps 提权 + 容错写入
  resources/
    desktop-zh-CN.json
    frontend-zh-CN.json
    statsig-zh-CN.json
assets/icon.ico       窗口图标（黑底白弧靶心）
build.py              PyInstaller onedir 打包脚本
CC_Chinese.spec       PyInstaller 配置
requirements.txt      运行依赖
```

---

## 状态机

```
待汉化 ──→ 汉化中（瞬态，打补丁时）──→ 已汉化
   ↑                                  │
   └──────────── 恢复 ─────────────────┘
未找到安装目录时禁用主按钮，通过「设置」手动指定路径
```

---

## 风险提示

- 本项目会修改本机已安装的 Claude Desktop 资源文件，请确认接受本地补丁与备份恢复的风险。
- 不建议在公司受管设备上绕过组织策略使用。
- Claude Desktop 更新后，补丁可能失效，需重新运行安装。

---

## 致谢

感谢原作者 **Jyy1529（Jash）** 的 [claude-desktop_win-zh_cn](https://github.com/Jyy1529/claude-desktop_win-zh_cn) 项目，
本项目的核心汉化逻辑（JSON 资源、chunk 补丁、restore 备份）均来源于原版。
