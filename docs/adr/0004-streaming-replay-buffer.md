# SSE 流式输出使用按任务维护的重放缓冲区

每次流式生成都会创建一个 `GenerationJob`，相关逻辑位于 `backend/app/streaming/manager.py`。任务拥有独立的 stream id，并维护一个有界的内存事件环形缓冲区，规模约为 5000 条事件。

前端通过 Server-Sent Events 消费流。如果连接中断并重连，客户端会发送 `Last-Event-ID`，服务端从该位置重放事件，前端再做去重。相关代码位于 `frontend/src/lib/sse.ts` 和 `frontend/src/stores/chat-store.ts`。

这样设计的原因：

- 长任务可能持续数分钟，Tauri WebView、移动浏览器和远程隧道都可能在真实环境中断开。
- 没有恢复能力的 SSE 会在每次抖动后被迫重新生成。
- WebSocket 也需要自行实现恢复协议。
- 带缓冲的 SSE 对 HTTP 代理和隧道友好，能在不重新执行工具的前提下恢复输出。
- 缓冲区有上限，避免内存无界增长。
