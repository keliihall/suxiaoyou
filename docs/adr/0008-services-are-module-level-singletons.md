# 长生命周期服务使用模块级单例，不挂在 app.state 上

`stream_manager`、`index_manager`、`provider_registry`、`tool_registry` 等长生命周期服务，使用模块级单例提供访问，例如 `from app.streaming import get_stream_manager`。

FastAPI lifespan 仍然负责构建和关闭这些服务，但会把它们注册到各自所属模块，而不是只挂在 `app.state` 上。

这样设计是为了配合 ADR-0007。Manager 不应该依赖 FastAPI 的 `Request` 或 `app.state`。当 Manager 执行多步操作时，例如 `delete_session_cascade(db, session_id)`，它需要直接调用 `stream_manager.abort_session()` 和 `index_manager.cleanup_session()`，而不引入 HTTP 层依赖。

## 影响

- 测试可以通过模块提供的 setter 替换单例，例如 `set_stream_manager_for_tests(...)`。
- 少数需要更细粒度控制的单元测试，可以在 Manager 调用点注入依赖。
- 后台 worker 和 CLI 入口没有 FastAPI app，也可以在自己的启动流程中构建并注册同一批单例。
