# 架构决策记录索引

本目录记录已经采用或明确延期的跨模块架构决策。ADR 说明约束和取舍，不能替代发布合同、实现或
验收证据；版本状态以 `docs/releases/` 中的对应文件为准。

| ADR | 决策 |
| --- | --- |
| [0001](0001-tauri-spawns-backend-subprocess.md) | Tauri 拉起本地后端子进程 |
| [0002](0002-sqlite-wal-default-store.md) | 默认使用开启 WAL 的 SQLite |
| [0003](0003-messages-are-parts.md) | 消息由持久片段序列组成 |
| [0004](0004-streaming-replay-buffer.md) | SSE 使用任务级重放缓冲区 |
| [0005](0005-compaction-is-a-persistent-part.md) | 上下文压缩作为持久片段保存 |
| [0006](0006-chatchannel-vendortransport.md) | ChatChannel 与 VendorTransport 分工 |
| [0007](0007-route-module-thin-api-layer.md) | API 保持为薄路由层 |
| [0008](0008-services-are-module-level-singletons.md) | 长生命周期服务使用模块级单例 |
| [0009](0009-context-window-prompt-assembler-extraction.md) | 抽出 ContextWindow 与 PromptAssembler |
| [0010](0010-native-dialog-extraction-deferred.md) | 暂缓抽出 NativeDialog |
| [0011](0011-runtime-events-and-turn-checkpoints.md) | v1.1 统一运行时事件与 turn checkpoint |
| [0012](0012-rewind-and-git-worktrees.md) | v1.1 rewind 与 Git worktree 的边界 |
| [0013](0013-hooks-and-acp-boundary.md) | v1.1 Hooks 与 ACP 共用事件语义但不共用权限 |
| [0014](0014-office-rendering-templates-and-visual-validation.md) | v1.1 Office 本地渲染、模板和视觉验证闭环 |
