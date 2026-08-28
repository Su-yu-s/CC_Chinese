# CC_Chinese 汉化修复设计

日期：2026-08-28

## 目标

补齐 Claude Desktop 中动态加载或 JavaScript 硬编码的中文文案，修正已知术语误译，并保证同一执行模式在菜单与底部快捷按钮中使用完全一致的名称。修改不得破坏顶部导航的原生 SVG 图标，也不得误改用户内容、项目名称、文件路径、API 标识或其他具有不同语义的同名术语。

## 采用方案

采用“静态资源优先、bundle 精确替换补齐、受限 DOM 运行时兜底”的三层方案。

1. 静态资源层修正 `core/resources/frontend-zh-CN.json` 和必要的桌面资源值。它负责已有哈希资源键对应的错误翻译。
2. bundle 层在 `core/patch_chunks.py` 中按完整英文字符串或带属性上下文的片段替换动态硬编码文案。不得为 `Code`、`Thread`、`Fork`、`Gateway` 等多义词增加无上下文的全局替换。
3. DOM 运行时层处理异步渲染、bundle 文件名变化或静态字符串被拆分的界面。运行时只遍历文本节点，并根据菜单、导航、设置卡片或输入区的容器上下文执行替换；不得修改 `innerHTML`，因此保留相邻 SVG 和事件处理器。

未采用的方案：

- 仅改 JSON：无法覆盖本次清单中的动态设置、模式菜单和输入区硬编码文案。
- 全局英文字符串替换：实现简单，但会误改 Claude Code 品牌名、用户项目名、Git Fork、Slack Thread、路径和配置字段，风险不可接受。

## 文案规范

### Claude Code 设置页

| 英文 | 中文 |
|---|---|
| Connect new sessions to Remote Control | 新会话自动连接远程控制 |
| Sessions you start on this computer connect to Remote Control automatically, so you can continue them from the terminal or claude.ai/code. | 在此电脑上启动的会话将自动连接远程控制，以便你可以从终端或 claude.ai/code 继续操作。 |
| Archive inactive sessions | 自动归档闲置会话 |
| Automatically archive local sessions after a period of no activity... | 一段时间无活动后自动归档本地会话。正在运行或有后台工作的会话不会被归档，包含未提交更改的工作树将保留在磁盘上。 |

第二条描述可能因 Claude 版本产生标点或后半句差异。bundle 层优先匹配完整版本；运行时层以设置标题为锚点，仅替换同一卡片的描述，不使用页面级前缀替换。

### 用户菜单

| 英文 | 中文 | 上下文限制 |
|---|---|---|
| Gateway | 第三方API | 仅齿轮用户菜单项；用户项目名和其他普通文本保留英文 |
| Inference configuration | 模型配置 | 仅用户菜单/对应设置入口 |

### 顶部导航

| 英文 | 中文 |
|---|---|
| Cowork | 办公 |
| Code | 编码 |

只替换顶部导航按钮的文本节点或 `label:` 属性字符串。`Claude Cowork`、`Claude Code` 品牌名及说明文字中的专有名词不变。禁止替换整个按钮 HTML，以保留任务列表 SVG 和 `</>` SVG。

### 输入区

| 英文 | 中文 |
|---|---|
| Type / for commands | 按 / 打开命令面板 |
| Bypass permissions | 最高权限 |

`Bypass permissions` 的“最高权限”仅用于执行模式名称、底部快捷按钮及其提示条标签。安全提示、设置说明和错误信息中的“绕过权限模式”继续使用描述性译法，避免改变安全语义。

### 执行模式

执行模式名称由一份规范映射驱动，菜单和底部快捷按钮不得各自维护不同译名。

| 英文名称 | 规范名称 | 描述 |
|---|---|---|
| Mode | 执行方式 | 菜单标题 |
| Manual | 每次问我 | 改任何东西前都先问你 |
| Accept edits | 自动修改 | 自动改，不用你管 |
| Plan | 计划模式 | 先制定计划，确认后再执行修改 |
| Bypass permissions | 最高权限 | Claude 可自动执行任何操作，不再询问 |

实现时同时覆盖原始英文、现有错误中文“接受编辑”和“绕过权限”两个短标签，但只在已识别的模式菜单或底部模式控件中生效。

### 术语修正

| 英文 | 中文 | 上下文限制 |
|---|---|---|
| Transcript view | 对话记录视图 | 对话菜单及对应静态资源键 |
| Artifacts | 作品 | 代码/文档预览区及相关可见 UI；内部标识和路径不变 |
| Thread | 对话 | 左侧会话列表语境；Slack、评论和编程线程不变 |
| Temperature | 随机性 | 模型设置项 |
| System Prompt | 预设指令 | 高级模型设置项及同一设置中的动作文案 |
| Fork | 分支对话 | 对话菜单；Git 仓库 Fork 不变 |

`Documents/Claude/Artifacts` 等真实文件路径必须保持原样。`Artifact`/`Artifacts` 不再通过无边界的页面级正则统一替换为“工件”，而是使用静态资源修正和预览区上下文替换为“作品”。

## 代码修改范围

### `core/resources/frontend-zh-CN.json`

- 修改能明确定位的哈希资源值，包括“成绩单视图”“系统提示”“分叉...”和相关对话菜单短标签。
- 修正预览区可见的“工件/Artifacts”术语，但保留含真实 `Artifacts` 文件夹路径的字符串片段。
- 不全局替换“线程”“代码”“计划”“温度”等普通词。

### `core/patch_chunks.py`

- 更新运行时规范文本映射，顶部导航改为“办公/编码”。
- 移除或收窄 `Artifact(s)` 的无上下文 substring 替换。
- 增加设置卡片、用户菜单、顶部导航、输入区和模式菜单的精确或容器限定替换。
- bundle 补丁使用完整字符串、`label:`/`title:` 等结构片段或不存在文件名触发的内容扫描机制，以兼容哈希文件名变化。
- 新增的运行时逻辑继续遵守现有扫描数量、节流和 MutationObserver 性能约束。

### `core/restore.py`

- 为新增的 bundle 结构替换增加可逆映射或残留清理。
- 静态 zh-CN 资源文件在恢复时直接删除，不需要逐键反向修改。
- 运行时注入由已有注入区块恢复机制移除；新增代码必须位于既有标记区块内。

## 数据流

1. `installer.run_install` 先调用 `patch_json.run_patch` 写入 zh-CN 资源。
2. `patch_chunks.run_patch_chunks` 扫描当前 Claude `ion-dist/assets` 下所有版本目录。
3. 精确 bundle 替换修正文案并备份首次修改前的文件。
4. 入口 bundle 注入或更新带标记的运行时脚本，异步界面出现后按上下文修正文案。
5. `detector.build_status` 按现有逻辑确认资源与白名单，无需改变状态模型。
6. 恢复时优先还原备份，再执行可逆替换和已知残留清理，最后移除 locale 与运行时配置。

## 错误与兼容策略

- 某个英文字符串在新版本 bundle 中不存在时跳过该项，不使整个汉化失败。
- 运行时找不到期望容器时不进行宽泛替换。
- 同一节点已经是目标中文时保持幂等，不重复修改。
- 运行时文本观察器不得处理 `script`、`style`、`contenteditable`、用户输入内容或项目名称节点。
- 所有专有名词按清单保留：Claude、Remote Control、Claude Code、Claude Cowork、claude.ai/code。

## 测试与验收

扩展隔离测试构造带代表性字符串的假 Claude bundle，至少验证：

1. 两项 Claude Code 设置标题和描述被翻译。
2. 用户菜单 `Gateway`/`Inference configuration` 被正确翻译，同时普通项目名 `Gateway` 不被替换。
3. 顶部导航得到“办公/编码”，bundle 中的 `Claude Code` 和 SVG 字符串保持不变。
4. 输入提示与模式菜单名称正确，菜单和底部快捷按钮均来自同一规范译名。
5. “成绩单视图”“工件”“系统提示”“分叉”在指定上下文得到纠正。
6. Git Fork、Slack Thread、真实 `Documents/Claude/Artifacts` 路径不变。
7. 重复安装幂等，不重复注入运行时区块。
8. 既有 `tests/test_patch_logic.py` 继续通过。

验收以用户提供清单中的最终可见文案为准；若 Claude 新版本改变 DOM 结构，允许 bundle 精确替换仍生效，运行时兜底安全跳过而不误改其他内容。
