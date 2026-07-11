# ChatChannel 负责聊天类渠道通用流程，VendorTransport 负责厂商协议

聊天类消息渠道共享大量基础流程，例如 webhook 分发、去重、白名单、流式卡片缓冲、媒体处理和文本分片。

这些通用流程抽到 `ChatChannel(BaseChannel)` 中，并通过声明式 `ChatProfile` 描述厂商差异，例如传输方式、编辑节流和签名机制。

厂商特有行为通过注入的 `VendorTransport` 协议提供，包括认证、发送、编辑、解析入站消息和上传等。这样采用组合而不是继承，每个厂商都可以用 `RecordingTransport` 做单元测试。

不符合聊天形态的渠道，例如邮件、RSS、语音或批处理渠道，继续直接继承 `BaseChannel`。

## 已考虑方案

- **单动词 Channel 转换器**：把渠道设计成厂商传输和消息总线之间的单个 `bind()` 转换器。放弃原因是微信扫码、交互登录、反应和命令会被迫绕进消息总线，与现有模式冲突。
- **多协议组合**：拆成 `WebhookVerifier`、`CredentialSource`、`MessageReceiver`、`MessageSender`、`DeltaRenderer`、`LifecycleHook` 等协议。放弃原因是每个协议深度不足，状态会分散到太多地方，当前渠道形态不足以支撑这种复杂度。
