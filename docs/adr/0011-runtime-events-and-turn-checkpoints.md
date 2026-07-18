# v1.1 使用统一运行时事件和持久 turn checkpoint

状态：已接受，随 v1.1 实施

v1.1 的工具调用、子 Agent、Hooks、ACP、Office 验证和 rewind 使用同一套版本化事件语义。SSE、ACP
通知和界面状态是这些事实的投影，不是新的事实来源。事件至少包含单调排序所需的 `event_id`、
`event_version`、`sequence`、`occurred_at`，以及可用时的 `session_id`、`root_turn_id`、`message_id`、
`call_id`、`checkpoint_id` 和 `workspace_instance_id`。未知事件版本必须 fail-closed，不能被当成已完成。

`root_turn_id` 从一个用户提示开始，覆盖它触发的全部步骤、工具调用、子 Agent 和最多两轮验证修复。
重试可以拥有新的 `call_id`，但不能伪装成新的用户 turn。后台任务如果不是由用户 turn 触发，必须使用
独立的根执行 ID，不能借用最近一次 turn。

v1.1 的规范生命周期至少包含：

```text
TurnStarted -> CheckpointPrepared -> ToolRequested -> PreToolUse
            -> ToolStarted -> ToolSucceeded|ToolFailed
            -> WorkspaceCommitted -> ValidationStarted -> ValidationCompleted
            -> CheckpointFinalized -> TurnCompleted|TurnStopped
```

`UserPromptSubmit`、`SessionStart`、`SubagentStart`、`SubagentStop`、`PreCompact` 和 `PostCompact` 是同一
事件族中的可观察节点。不是每个 turn 都会发生工具提交或验证；发生过工作区提交的 turn 则必须有且
只有一个最终 checkpoint 结果。

## Checkpoint 合同

checkpoint 是 rewind 的持久恢复点，不是 UI 书签。它必须记录：

- 对话前沿、待办和 Goal 运行引用，以及该 turn 启动的子 Agent 引用；
- 每个本地目标的规范路径、变更类型（创建、修改或删除）、变更前版本引用和提交后 SHA-256；
- 目录创建/删除和 Office 多文件提交所需的完整 mutation ledger；
- 已发生但不可逆的外部副作用摘要，只包含来源、操作类别和审计 ID，不复制凭据或敏感正文；
- checkpoint 状态 `prepared`、`committing`、`finalized`、`rewinding`、`rewound` 或 `failed`。

工作区事务成功后，只有 mutation ledger 和 `WorkspaceCommitted` 已经持久化，才能向调用方报告工具提交
成功。文件写入成功但 ledger 未落盘属于未完成提交，启动恢复必须处理它。新建文件没有“变更前版本”，
因此必须显式记录 `created`，不能用空版本引用代替。

数据库事务和文件系统无法共享单一 ACID 事务。实现必须使用持久 journal 和幂等恢复步骤，使崩溃后的
可观察结果最终收敛到完整前态或完整后态；不能只依赖进程内回滚。版本保留和垃圾回收必须 pin 所有尚可
rewind 的 checkpoint 引用。

## 安全与数据边界

- checkpoint 只覆盖由苏小有受控事务提交的本地状态。邮件、MCP、云文档、远程 API、付费调用和 Git
  push 等副作用只记录为不可逆，不宣称撤回。
- 事件载荷不得包含密钥、原始认证 Header 或未经脱敏的 Hook 环境；持久审计仍是安全事实来源。
- Hooks、ACP 客户端和验证 Agent 可以观察经过授权的事件投影，但不能写入伪造的完成事件或提升权限。
- 现有消息编辑产生的“删除后续消息并重新生成”只有接入 checkpoint 恢复后，才能在产品中称为 rewind。

## 验收影响

实现需要提供事件序列契约测试、重复投递和乱序投影测试、并发 turn 隔离测试，以及在 journal 的每个
持久化边界进行崩溃注入的恢复测试。只检查消息删除或单个文件版本恢复，不能证明本 ADR 已完成。
