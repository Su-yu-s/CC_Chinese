# CC_Chinese 全程管理员与免安装版实施计划

规格：`docs/superpowers/specs/2026-09-02-cc-chinese-always-admin-portable-design.md`

## 1. 先更新发布守卫

- 修改 `tests/test_product_trust_guards.py`，要求用户可见名称为 `CC_Chinese`，构建参数包含 `--uac-admin`。
- 扩展 `tests/test_elevation_guards.py`，覆盖快照根首次安全创建、合格目录复用、不安全目录拒绝。
- 扩展 GUI 测试，确认 WindowsApps 汉化/恢复由管理员主进程直接调用，不再触发二次提权代理。

## 2. 实现管理员主进程路径

- 在 `build.py` 启用 PyInstaller `--uac-admin`，让生成的 `CC_Chinese.spec` 固化 `uac_admin=True`。
- 修改 `main.py`：产品名统一为 `CC_Chinese`；WindowsApps 操作在当前管理员 Worker 中直接调用事务安装器；非管理员源码运行时明确拒绝。
- 保留提权代理兼容入口，但从 GUI 活动路径移除 Program Files 分发位置判断。

## 3. 支持免安装版初始化快照根

- 在 `core/elevation.py` 增加仅管理员可调用的安全初始化函数。
- 目录不存在时创建并设置受保护 ACL，随后复检；目录已经存在但可由普通用户写入时拒绝。
- 在 `core/installer.py` 的首次 WindowsApps 快照操作前调用初始化函数，确保任何文件修改之前完成安全门禁。

## 4. 统一分发名称和说明

- 修改 `CC_Chinese.iss` 的 AppName、Publisher、快捷方式和卸载提示。
- 修改 `README.md`，说明安装版与免安装版都会在启动时申请管理员权限。
- 更新与本次最终决策冲突的静态说明和测试。

## 5. 回归与打包

- 运行管理员入口、快照事务、失败回滚、翻译修复、状态真实性和 GUI 测试。
- 重新运行 `py -3.13 build.py`，读取 PE 清单确认 `requireAdministrator`。
- 运行冻结版 `--self-test`，再用 Inno Setup 编译安装包。
- 记录两个产物的大小、时间和 SHA256；测试期间不修改真实 Claude 文件。
