# 恢复安全与翻译可靠性实施计划

对应规格：

- `docs/superpowers/specs/2026-08-31-restore-safety-and-translation-repair-design.md`
- `docs/superpowers/specs/2026-09-01-product-trust-and-continuous-compatibility-design.md`

## 1. 建立共享安全 I/O 与快照基础设施

涉及文件：

- 新增 `core/safe_io.py`
- 新增 `core/backup_manifest.py`
- 新增 `tests/test_safe_io.py`
- 新增 `tests/test_backup_manifest.py`

步骤：

1. 先写失败测试，覆盖同目录原子写、替换失败保留原文件、临时文件清理。
2. 实现文本、字节和复制三种原子写接口。
3. 定义 install-id、manifest schema、路径归一化与 SHA-256 工具。
4. 实现 staging/sealed/complete/invalid 快照、完整 resources 相对路径 payload、配置原值和 security descriptor 元数据记录。
5. 测试 install-id 汉化前后稳定、资源布局变化后失效、路径逃逸拒绝。

## 2. 将 JSON 补丁接入事务快照

涉及文件：

- `core/patch_json.py`
- `core/installer.py`
- `tests/test_patch_logic.py`
- 新增 `tests/test_install_transaction.py`

步骤：

1. 将 JSON 目标、英文回退、白名单和硬编码候选暴露为 mutation plan。
2. 所有目标写入前调用统一备份门禁；备份失败返回失败且不写目标。
3. `set_locale()` 改为由 manifest 保存首次原值，并使用原子写。
4. 重复安装不得覆盖首次官方基线或配置原值。
5. installer 在 JSON/chunk 之间共享同一事务，任一阶段失败时回滚。

## 3. 修复 chunk 备份、写入门禁和多 assets 目录

涉及文件：

- `core/patch_chunks.py`
- `tests/test_chunk_cache.py`
- `tests/test_translation_repairs.py`
- 新增 `tests/test_chunk_backup_safety.py`

步骤：

1. `backup_file()` 返回布尔结果并保存相对 app/resources 的完整路径。
2. `_apply_replacements()`、字体运行时和会话运行时在备份失败时停止写入并上报失败。
3. 所有 JS 写入切换到共享原子写接口。
4. 用 v1/v2 同名 index 夹具验证备份互不覆盖、恢复路径精确。
5. 保持 chunk 缓存、稳定前缀和性能路径幂等。

## 4. 重写安全恢复预检与提交

涉及文件：

- `core/restore.py`
- `core/backup_manifest.py`
- `core/installer.py`
- 新增 `tests/test_restore_safety.py`
- 更新 `tests/verify_patch_restore.py`

步骤：

1. 删除无条件 `,"zh-CN"` scrub。
2. 新恢复入口只接受与当前 install-id 匹配的 complete manifest。
3. 恢复仅遍历 manifest 文件清单；禁止绝对路径和 `..`。
4. `existed=true` 原子恢复 payload；`existed=false` 仅在补丁哈希匹配时删除。
5. locale/font 根据存在性和 JSON 原值精确恢复。
6. 恢复提交前暂存当前状态，阶段失败时回滚到恢复前字节。
7. legacy flat backup 返回不兼容错误且零写入。
8. 验证官方原生 zh-CN 数组字节不变、patch-state 不泄漏、A→B 拒绝。

## 5. 修复检测器与 GUI 状态真实性

涉及文件：

- `core/detector.py`
- `core/installer.py`
- `main.py`
- `tests/test_freeze_guards.py`
- 新增 `tests/test_detector_backup_status.py`

步骤：

1. 以当前 app_dir 查询 `ready/missing/legacy_incompatible/mismatch/corrupt` 备份状态。
2. UI 检查项显示匹配状态，不再用目录非空判断“已准备”。
3. installer 安装完成后运行真实 detector 验证；失败则回滚。
4. `_on_action_done()` 删除强制 `localized=True`，统一异步重检。
5. 测试成功回调不会直接显示已汉化，最终状态服从 detector。

## 6. 修复五项 React-Intl 翻译与 artifactLabel

涉及文件：

- `core/resources/frontend-zh-CN.json`
- `core/patch_chunks.py`
- `tests/test_translation_repairs.py`

步骤：

1. 修改五个侧栏消息 ID 为指定文案。
2. 添加 `artifactLabel="Artifacts"` 与旧译名“工件”的精确规则。
3. 增加仅限侧栏上下文的英文/旧中文 DOM 映射，不全局替换“新/自定义”。
4. 测试真实消息 ID、旧 bundle、正反向恢复和路径/项目名/SVG 防误替换。

## 7. 缩小 WindowsApps 权限范围

涉及文件：

- `core/best_effort_io.py`
- `core/installer.py`
- `core/patch_json.py`
- `core/patch_chunks.py`
- 新增 `tests/test_permission_scope.py`

步骤：

1. installer 只读生成封闭 mutation plan，先封存 baseline，再执行一次精确权限事务。
2. 记录并精确还原目标文件和创建目标所需父目录的 owner/DACL/继承/属性，不对 resources 使用 `/r` 或 `/t`。
3. 单独处理超时并实际复核可写性；权限、补丁、恢复失败与成功退出时均验证 security descriptor 已还原。
4. 删除 JSON 与 chunk 阶段重复的递归权限调用。
5. WindowsApps 的提权子流程只允许从受保护 Program Files 发行版启动；patch 与 restore 在提权进程内独立重验，不接受父进程 mutation plan 或用户可写 payload。
6. ProgramData 管理员快照根使用固定 owner/DACL，拒绝弱 ACL、普通用户可写对象和 reparse point。

## 8. 修复操作期间关闭窗口

涉及文件：

- `main.py`
- `tests/test_freeze_guards.py`

步骤：

1. busy 状态下 `closeEvent` 调用 `event.ignore()` 并给出提示。
2. 非 busy 状态保持即时关闭。
3. 移除把 `requestInterruption()` 当作取消完成的行为。
4. 测试关闭处理非阻塞、任务继续、完成后可关闭。

## 9. 完整验证与重新打包

步骤：

1. 运行所有新增及既有隔离测试。
2. 运行 Python 编译检查、GUI offscreen 冒烟和 patch→detect→restore 字节级往返。
3. 对当前可发现 Claude 仅做只读检测；若不存在则明确记录，不伪造实机验证。
4. 使用现有 Python 3.13/PySide6/PyInstaller 环境运行 `build.py`，不安装环境。
5. 校验打包源码与工作树一致、Qt6Core/QtCore 存在、五项消息 ID 和安全恢复代码进入 dist。
6. 输出最终 EXE 路径、大小和 SHA-256。

## 10. 兼容性门禁与基线判定

涉及文件：

- 新增 `core/compatibility.py`
- `core/installer.py`
- `core/detector.py`
- 新增 `tests/test_compatibility.py`

步骤：

1. 将支持布局定义为唯一可匹配的 profile，枚举候选文件集合、关键消息 ID、每条规则的精确命中和冲突条件。
2. 在首次快照、权限变更和补丁写入前，区分 `verified/unsupported/unknown`；拒绝路径除脱敏日志外严格零副作用。
3. 官方资源参与 clean 判定；locale/字体等用户配置允许任意 schema-valid 原值并逐值保存。
4. complete manifest 优先判定 `restored_with_manifest/expected_patched/recovery_incomplete/partial_or_foreign`，禁止覆盖首次基线。
5. installer 最终结果同时要求 profile、完整 manifest、资源与运行时检测通过。

## 11. 产品状态真实性、日志与普通权限启动

涉及文件：

- `main.py`
- `core/diagnostics.py`
- `core/installer.py`
- `core/detector.py`
- `build.py`
- `CC_Chinese.iss`
- `README.md`
- GUI 与日志测试

步骤：

1. 删除假“检查更新”、无效更新通道和可关闭备份开关，保留真实 GitHub/Issues。
2. 用户可见名称统一为“Claude Desktop 中文助手”，修正“AppData 本地安装版”。
3. 移除 `--uac-admin`，保留 Program Files 受保护的机器级安装；只在 WindowsApps 写操作时按需提权，便携 dist 禁止提权。
4. 加入首次使用披露，未确认时不执行写操作。
5. 定义稳定错误码、可复制详情、打开日志；日志脱敏并采用 5 × 2 MiB 轮转，零上传。
6. 当前卸载器只移除助手并明确提示“不等于恢复 Claude”，不调用旧 restore、不删除快照。
7. GUI 所有成功状态来自异步重检，不信任子流程退出码或结果文件。

## 12. 提权安全与防篡改验证

涉及文件：

- 新增或扩展 `core/elevation.py`
- `core/best_effort_io.py`
- `core/installer.py`
- `main.py`
- 新增 `tests/test_elevation_security.py`
- 更新 `tests/test_permission_scope.py`

步骤：

1. 提权 CLI 只接受枚举动作、目标定位提示和 nonce；拒绝任意命令、源路径、文件列表和父进程计划。
2. 验证当前 exe、相邻运行时、ProgramData 快照根与目标均位于允许位置且不是 reparse point。
3. patch 子流程重新验证 profile/基线并生成封闭计划；restore 子流程只验证 complete manifest/install-id/payload/目标哈希。
4. 每次原子替换前重验目标哈希，UAC 间隙漂移立即停止并事务回滚。
5. 结果文件只用于诊断；GUI 仍以 detector 重检作为状态真相。
6. 覆盖 exe/DLL、nonce、目标、manifest/payload、ACL 和 reparse 篡改测试。

## 13. 发布验收

1. 逐项执行两份规格的缺陷—修复—验证矩阵，不能以单个绿测替代完整要求。
2. 验证旧扁平备份保留但永不自动恢复，现有用户目录不被清理。
3. 验证五项实际消息 ID 与侧栏运行时兜底，同时确认项目名、路径、SVG 等防误替换保护。
4. 验证普通启动无 UAC、正式安装版按需提权、便携版拒绝管理员写入。
5. 验证当前卸载不触碰 Claude 或快照。
6. 全量测试、编译检查、offscreen GUI、PyInstaller onedir 与 Inno 安装包均成功。
7. 对最终 exe 和安装包计算 SHA-256，记录构建时间、大小和可复现源码提交。
