# 从 SessionPrompt 中抽出 ContextWindow 和 PromptAssembler，保持步骤循环不变

`backend/app/session/prompt.py` 里原本混合了多个职责：模型和服务商解析、系统提示词组装、消息持久化、上下文预算和压缩、步骤重试与继续策略。

本决策只抽出两个职责，其它步骤循环相关逻辑继续留在原位置。

## ContextWindow

`ContextWindow` 负责上下文窗口适配流程：

```text
microcompact_messages -> apply_tool_result_budget -> context_collapse -> LLM 压缩
```

公开接口为：

```text
fit(messages, scheduled_tools, *, on_summarize, token_counter) -> FitOutcome
```

`ContextWindow` 是按会话持有状态的模块，会在内部保留上下文折叠耗尽和连续压缩失败等断路器状态，但不把这些字段暴露到接口外。

## PromptAssembler

`PromptAssembler` 是纯函数，接收已经解析好的输入并返回结构化提示词，而不是平铺字符串。这样可以为支持提示词缓存的模型放置缓存断点。

工作区记忆、全文检索状态、项目指令、技能列表、当前时间和平台名称等 I/O 仍由 `SessionPrompt._setup()` 负责解析，纯函数只消费结果。

实际实现落在 `app.session.system_prompt` 中。该模块保留 `build_system_prompt` 作为便利入口，同时提供更利于测试的 `assemble(...)`。

## 不抽出 LoopController

待办恢复、继续次数和 microcompact 决策与步骤循环状态耦合很深。若强行抽出，会形成泄漏接口，因此本轮不抽出 `LoopController`。

## 已考虑方案

- **注入多种 BudgetPolicy、CompactionStrategy、Compactor 和 PromptSource 协议**：灵活度过高，但多数接口还没有足够生产适配器，会形成假扩展点。
- **把 Provider 直接注入 ContextWindow**：会让单元测试依赖真实服务商，不利于无网络测试。
- **PromptAssembler 只返回字符串**：会丢失提示词缓存断点能力。

## 影响

- 每个会话构建一次 `ContextWindow`，并在步骤循环中复用。
- `on_summarize` 回调由 `SessionPrompt` 构建，捕获服务商、代理、模型和压缩前工作区记忆排队逻辑。
- `FitOutcome.summary_metadata` 仅作为透传字段，调用方负责记录。
- 迁移顺序为先抽 `PromptAssembler`，再抽 `ContextWindow`，降低步骤循环语义风险。
