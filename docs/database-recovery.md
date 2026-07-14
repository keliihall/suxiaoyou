# 数据库升级与离线恢复

v0.9.0 在每次数据库升级前创建 SQLite online backup，并为备份写入包含
应用版本、数据库路径、SHA-256、文件大小、源/目标 Alembic revision 和创建时间的清单。恢复命令会先校验
清单、校验和、SQLite `quick_check` 与 revision，并将表字段的类型、非空约束、默认值、主键位置、
外键、唯一约束和索引与该 revision 的正式 schema 逐项比对。

恢复时只读取一次备份文件：写入私有临时数据库的同时计算大小和 SHA-256，并仅在二者都与清单一致后继续。
因此，校验后对原备份文件的修改不会改变待安装内容；若修改发生在读取前或读取期间，恢复会失败并保留
正式数据库和安全备份。通过全部校验后，后端才会原子替换正式数据库；不会在正式数据库上原地降级。
替换前还会保留一份 `pre-restore` 安全备份。

后端服务无法启动时，可直接运行最终安装包内的后端可执行文件：

```bash
suxiaoyou-backend --data-dir "/path/to/app-data" --list-backups
suxiaoyou-backend --data-dir "/path/to/app-data" --restore-backup "/path/to/suxiaoyou.db.pre-v0.9.0-<timestamp>.bak.json"
```

源码环境使用同样的参数：

```bash
backend/.venv/bin/python backend/run.py --data-dir "/path/to/app-data" --list-backups
backend/.venv/bin/python backend/run.py --data-dir "/path/to/app-data" --restore-backup "/path/to/backup.bak.json"
```

非默认数据库可额外传入完整 URL，例如
`--database-url sqlite+aiosqlite:////absolute/path/suxiaoyou.db`。清单缺失、校验和不符、
数据库损坏、revision 不匹配或来自当前版本不认识的 schema 时，恢复会拒绝执行。

## 进程互斥与支持边界

正式后端会在迁移开始前取得数据库的 OS 级独占租约，并一直持有到服务停止且数据库 engine 完成
`dispose`。`--list-backups` 和 `--restore-backup` 也必须独占取得同一租约；若应用或另一个恢复进程仍在
使用数据库，命令会直接失败，不会等待或继续替换。这保证受支持的苏小有后端进程不会在恢复检查完成后
再提交写入，也不会丢失另一个应用进程的已提交数据。

执行离线恢复前必须完全退出应用。该租约用于协调苏小有正式后端和随安装包提供的恢复命令；绕过后端、
直接使用 SQLite 客户端或其他不遵守租约的程序写数据库不在支持范围内。不要把租约视为针对恶意本地程序的
安全边界。

## 回滚到旧版本

顺序必须是：

1. 完全退出苏小有，确认没有后端进程仍在访问数据库。
2. 使用 v0.9.0 后端的 `--list-backups` 找到并验证 `pre-v0.9.0` 备份。
3. 使用 v0.9.0 后端的 `--restore-backup` 原子恢复该升级前备份。
4. 安装并启动与该备份 schema 匹配的旧版应用。

不要在恢复后再次启动 v0.9.0 再安装旧版；v0.9.0 会把未版本化或 v0.8.x 数据库重新
升级到 0005。若误启动，请退出应用并再次执行上述恢复流程。
