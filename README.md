# CC_Chinese

为 Windows 版 Claude Desktop 提供中文界面补丁。工具只修改本机界面资源，不读取聊天、项目或工作区内容。

> 本项目不是 Anthropic 官方产品。Claude Desktop 更新后资源结构可能变化；未通过兼容性检查的版本不会被修改。

## 普通用户

1. 可以运行 `CC_Chinese_Setup.exe` 安装，也可以直接使用 `dist/CC_Chinese/CC_Chinese.exe` 免安装版。
2. 双击 `CC_Chinese` 后会立即请求管理员授权；安装版和免安装版功能一致。
3. 点击“一键汉化”。
4. 操作结束后工具会重新读取真实文件；只有资源、运行时标记和 locale 全部通过校验才显示“已汉化”。

如需撤销，请在卸载助手前点击“恢复原样”。恢复只接受与当前 Claude 安装严格匹配、文件哈希完整的安全快照；不会拿旧版本备份覆盖新版本。

卸载助手不会自动修改 Claude，也不会删除保留在本机的安全快照。

## 当前能力

- 自动定位 WindowsApps 与 AppData 本地安装版 Claude Desktop
- 资源结构兼容性门控；未知版本停止写入
- 强制创建版本绑定的完整快照，备份失败即停止
- JSON、JS chunk 与配置文件原子写入
- 写入失败自动回滚；恢复失败回到恢复前状态
- locale/font 原值精确还原
- 五项侧边栏翻译：新建、项目、作品、定时、定制
- 失败详情复制与本地脱敏日志（最多 5 个文件，每个 2 MiB）
- WindowsApps 精确目标权限处理，不递归接管整个 `resources`

## 兼容性说明

工具不靠版本号猜测支持情况，而是核对关键资源、消息 ID、入口 bundle 数量和运行时标记。检测到未知布局时会提示“尚未适配”，不会尝试碰运气写入。

当前代码已在 Claude Desktop `1.40609.1.0` 的已安装资源布局上完成只读兼容性验证。后续版本是否可用以工具内实际检测结果为准。

## 出问题怎么办

- 先完全关闭 Claude 后重试。
- 操作失败时点击“复制详情”或“打开日志目录”。日志只记录版本、错误码等允许字段，不记录聊天正文、令牌和用户绝对路径。
- 如果 Claude 已被其他补丁修改，工具会拒绝创建“官方基线”；请先使用 Claude 官方修复/重装，再运行助手。
- 反馈地址：[GitHub Issues](https://github.com/Su-yu-s/CC_Chinese/issues)

## 开发与构建

要求 Python 3.13、PySide6、PyInstaller。项目不会在构建时自动安装或更换你的环境。

```powershell
py -3.13 -m pip install -r requirements.txt pyinstaller
py -3.13 build.py
```

onedir 产物：`dist/CC_Chinese/CC_Chinese.exe`

安装包（需要 Inno Setup 6）：

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" CC_Chinese.iss
```

安装包产物：`release/CC_Chinese_Setup.exe`

主要隔离测试：

```powershell
py -3.13 tests/test_backup_manifest.py
py -3.13 tests/test_compatibility.py
py -3.13 tests/test_translation_repairs.py
py -3.13 tests/verify_patch_restore.py
py -3.13 tests/test_installer_transaction.py
$env:QT_QPA_PLATFORM = "offscreen"
py -3.13 tests/test_freeze_guards.py
```

## 项目结构

```text
main.py                  GUI、状态机、管理员主进程入口
core/detector.py         安装定位与真实状态检测
core/compatibility.py    资源结构兼容性门控
core/backup_manifest.py  版本绑定快照与 manifest
core/safe_io.py          原子文件写入
core/best_effort_io.py   精确权限事务
core/installer.py        汉化/恢复事务编排与回滚
core/patch_json.py       JSON、locale 与白名单补丁
core/patch_chunks.py     chunk 翻译及运行时增强
core/restore.py          严格快照恢复命令入口
core/diagnostics.py      本地脱敏诊断
core/elevation.py        管理员校验与安全快照目录初始化
```

## 风险与边界

- 本工具会修改 Claude Desktop 的本地安装资源，可能不适用于受组织管理的设备。
- 请自行确认这种本地修改与适用于你的服务条款及组织政策的关系。
- 无代码签名的自构建安装包可能触发 SmartScreen；正式分发前建议使用可信代码签名证书。
- 当前版本不提供联网更新、后台监控或遥测；界面中也不会展示对应的假功能。这些能力属于后续路线图。

## 致谢

项目基于 [Jyy1529/claude-desktop_win-zh_cn](https://github.com/Jyy1529/claude-desktop_win-zh_cn) 的汉化思路继续开发，感谢原作者 Jyy1529（Jash）。
