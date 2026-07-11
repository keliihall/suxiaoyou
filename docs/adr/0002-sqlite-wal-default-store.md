# 默认使用开启 WAL 的 SQLite，PostgreSQL 仅作为可选项

苏小有默认使用 SQLite 持久化项目、会话、消息和片段，存储代码位于 `backend/app/storage/`。SQLite 会开启 WAL 模式并启用外键约束。

PostgreSQL 仍然可以配置，用于自托管或多人共享部署，但它不是默认路径。

选择 SQLite 的原因：

- 苏小有是本地优先的桌面端应用，用户数据默认留在自己的电脑上。
- SQLite 随 Python 环境可用，以单文件方式存放在应用数据目录中。
- WAL 对单用户、多会话和流式生成的并发读写已经足够。
- 保留 PostgreSQL 选项，可以让高级用户在共享部署中接入真实数据库，而不必重写数据层。
