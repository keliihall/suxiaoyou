# v1.1 Hooks 与 ACP 共用运行时事件语义，但不共用信任和权限

状态：已接受，随 v1.1 实施

Hooks 是本地策略/自动化扩展点，ACP 是外部客户端与 Agent runtime 的协议边界。两者消费 ADR-0011 的
统一事件投影，但都不能成为权限事实来源，也不能直接写入 checkpoint 或伪造工具完成状态。

## Hooks 决策

v1.1 GA 只支持应用内置 Hook 和经用户按内容 SHA-256 明确授权的项目/插件本地命令 Hook。支持事件为
`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`SubagentStart`、
`SubagentStop`、`PreCompact` 和 `PostCompact`。

- Hook 接收版本化 JSON，标准输出只接受有界、版本化 JSON 响应；日志进入单独的有界捕获区。
- `PreToolUse` 可以 `allow`、`deny` 或 `ask`，但 v1.1 不能改写工具名称、参数、工作区、来源或权限。
- 其它 Hook 只能返回注释或失败状态，不能回写工具结果。Hook 产生的新动作必须作为新的受审计调用重新
  进入权限管线。
- Hook 使用最小环境变量白名单，不默认继承父进程环境或凭据；工作目录固定为已授权工作区实例；超时、
  输出上限、取消和子进程树回收必须跨平台生效。
- 应用内置的必需策略 Hook 失败时 fail-closed。用户可选 Hook 超时、格式错误或退出失败时记录警告并
  fail-open，但不能改变原调用的已有权限判断；每个 Hook 必须在注册时声明失败策略。
- 项目首次启用、内容 SHA 改变或执行文件解析结果改变时重新授权。符号链接目标、最终可执行路径、参数、
  环境和工作目录都进入审计绑定。

HTTP Hook、参数重写、动态代码加载、秘密自动注入和后台常驻 Hook 不进入 v1.1。

## ACP 决策

ACP 在 v1.1 作为 Beta 提供。首版只支持本地 `stdio` 传输和实现清单锁定的公开 ACP 版本；不监听网络
端口。实现覆盖标准会话创建/加载、提示、取消、消息更新、计划更新、工具状态和交互权限请求。
正确性边界限定为一个持有整个 application lifespan 独占租约的单进程、file-backed SQLite 部署。
ACP CLI 必须进入与桌面端相同的 lifespan；内存 SQLite、其它数据库、多进程 worker 或不能取得租约
的组合必须在打开 stdio 之前拒绝。v1.1 不声称跨进程 idempotency 或多租户调度。

- ACP 会话映射到真实苏小有会话和 `root_turn_id`，不能建立绕过持久消息、审计或 checkpoint 的旁路；
- 客户端声明的能力只是协商输入，不授予权限。最终工具路径、来源级策略、用户确认、急停和审计继续由
  苏小有执行；断线或无法展示确认时 fail-closed；
- ACP 输出是运行时事件的投影。带 `messageId` 的 prompt 使用持久幂等 ledger，并把该 ID、
  请求摘要和真实持久化用户 `Message` 相互绑定。只有 ledger 和原消息文本都仍匹配时才能重放
  已完成响应，不能重复执行付费或写操作；
- history edit、delete 和 rewind 在修改/删除消息的同一数据库事务中将所有受影响 ACP ledger 标记
  为 `interrupted`。之后相同 `messageId` 必须 fail-closed，不能返回已与当前对话分叉的旧响应；
- prompt admission、桌面端 history edit 和会话删除共用 StreamManager 互斥边界。取消必须传播到
  生成、工具和子 Agent，并保留已有 checkpoint/journal 的恢复责任；不能通过终止 ACP
  进程跳过提交收尾；
- 取消超时只表示客户端等待截止，不表示执行已安全停止。runner、所有被跟踪 tool task 和
  manager-owned 收尾任务真正 quiescent 之前，会话仍被视为 active，删除会话必须返回冲突而不是
  提前释放后续工作；
- 所有客户端字符串、路径和富文本都按不可信输入处理。协议错误、未知版本和超限消息结束当前请求，
  不降低为内部 HTTP 接口；
- 现有 OpenAI 兼容 HTTP API 不是 ACP，也不能被列为 ACP 验收证据。

WebSocket/远程 relay、会话公开分享、云同步、ACP 自定义权限扩展和无人工值守的远程写入不进入 v1.1。

## 验收影响

Hooks 需要证明被拒工具产生零副作用、可选/必需失败策略、内容变更重授权、秘密脱敏、超时取消和进程树
回收。ACP 需要公开 SDK/协议一致性测试、真实客户端 smoke、file-backed SQLite lifespan lease 冲突测试、
断线权限 fail-closed、重复取消、顽固工具 quiescence、恢复、`messageId` 重放以及 edit/delete/rewind 后
必然拒绝重放的崩溃/并发测试，并覆盖 Windows/CJK 路径。任一入口可以扩大权限或绕过审计时，
两项能力都必须停止发布。
