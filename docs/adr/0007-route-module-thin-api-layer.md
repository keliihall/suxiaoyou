# backend/app/api 是薄路由层，多管理器编排放在 Manager 层

`backend/app/api/` 中的接口使用 `Route` 装饰器族表达，包括 `route.list`、`route.get`、`route.create`、`route.update`、`route.delete`，以及 `route.stream`、`route.multipart`、`route.custom` 等特殊入口。

认证、权限规则评估、`DomainError` 到 `HTTPException` 的映射和审计日志由路由模块统一处理。路由处理函数只表达要调用哪个 Manager 方法。

Manager 方法的类型签名是路由注入参数的事实来源。需要多步操作时，例如删除会话时中止流、删除上传、删除记录和清理索引，应收敛为一个 Manager 方法，而不是在路由层编排。

这样设计的目的，是让事务推理、业务一致性和跨管理器操作停留在 Manager 层。

## 已考虑方案

- **可插拔 Concern 栈**：为每个路由组合认证、权限、审计等 concern。放弃原因是 SSE 流式接口打破普通 before/after 模型，认知成本高于收益。
- **极简 route 加 raw 逃生口**：异常接口完全落回 FastAPI 原生 router。放弃原因是文件上传、导出和原生对话框这类接口反而最需要统一审计和错误映射。

## 影响

- 仍返回 `dict` 的接口需要逐步补充 Pydantic 响应模型。
- 长生命周期服务从 `app.state` 移到模块级单例，详见 ADR-0008。
- 迁移可以增量进行，`Route` 和普通 FastAPI 路由在过渡期可以共存。
- 测试可以通过 `TestRouteRegistry` 直接分发请求到 handler，不必启动完整 FastAPI 应用。

## 审计日志形态

统一采用 `key=value` 字段拼接进日志消息字符串，而不是依赖 `extra={...}`。标准库日志默认 formatter 不会自动渲染 `extra` 字段。

非流式路由在请求结束时输出一行：

```text
audit user={user} route={route} status_code={status} duration_ms={ms}
```

流式路由输出两行，并共享同一个 `stream_id`：

```text
audit.stream.open stream_id={id} user={user} route={route} started_at={ts}
audit.stream.close stream_id={id} outcome={completed|aborted|error} duration_ms={ms} [error_class={cls}]
```

不记录每个流式 chunk。审计层关注请求生命周期，不关注每个网络帧。

审计日志统一使用 `app.audit` logger。路由处理函数不要自行写 `logger.info("audit ...")`，业务事件日志应使用其它 logger 名称。
