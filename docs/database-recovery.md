# 数据库升级与离线恢复

v1.1.0 延续并强化每次数据库升级前的 SQLite online backup，为备份写入包含
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
suxiaoyou-backend --data-dir "/path/to/app-data" --restore-backup "/path/to/suxiaoyou.db.pre-v1.1.0-<timestamp>.bak.json"
```

源码环境使用同样的参数：

```bash
backend/.venv/bin/python backend/run.py --data-dir "/path/to/app-data" --list-backups
backend/.venv/bin/python backend/run.py --data-dir "/path/to/app-data" --restore-backup "/path/to/backup.bak.json"
```

非默认数据库可额外传入完整 URL，例如
`--database-url sqlite+aiosqlite:////absolute/path/suxiaoyou.db`。清单缺失、校验和不符、
数据库损坏、revision 不匹配或来自当前版本不认识的 schema 时，恢复会拒绝执行。

## 工作区身份 v2 升级边界

`0012_v110_workspace_identity_v2` 是一个有意保持空操作的 Alembic revision：它不从
SQLite 迁移外部文件系统状态，但会让升级流程在进入新存储协议前创建和校验
数据库备份。数据库打开后，后端才逐个迁移活跃工作区的旧 `stat-v1` 记录：

1. 先验证当前目录与旧身份是否能安全证明连续性；
2. 在 POSIX 根目录建立优先使用 xattr 的 `marker-v2` （不支持时使用安全标记文件），
   Windows 则读取 `winfile-v2` 原生卷序列号与文件 ID；
3. 把旧文件版本树复制到 schema 3 目标，并校验全部对象、pin 与 checkpoint 引用；
4. 再次确认工作区身份，最后才提交 SQLite 中的新 token。

因此崩溃发生在前三步时，旧数据库记录仍是权威状态，下次启动可幂等重试；
旧文件版本树也会保留。某个工作区缺失、被替换、身份不可证或历史损坏时，
后端会保留它的旧 token、journal 和 pin，只阻断该工作区的恢复；其他工作区和本地服务
仍可启动。不会因为“路径相同”就把旧 checkpoint 绑到新目录。

数据库备份只包含 SQLite，不包含工作区上的 xattr/标记文件或应用私有的文件版本树。
离线恢复到 `0011` 后若再启动 v2 后端，它会复用已建立的 durable token 并重试上述迁移；
若要回滚到旧应用，必须先按下文恢复对应的旧 revision 数据库。不要手工删除身份表示
或任何一份新旧文件版本树来“解锁”恢复。

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
2. 使用 v1.1.0 后端的 `--list-backups` 找到并验证 `pre-v1.1.0` 备份；v1.0.0 创建的
   `pre-v1.0.0` manifest 仍受支持。
3. 继续使用 v1.1.0 后端的 `--restore-backup` 原子恢复该升级前备份。
4. 安装并启动与该备份 schema 匹配的旧版应用。

不要在恢复后再次启动 v1.1.0 再安装旧版；v1.1.0 会把已恢复的旧 schema 重新升级到当前 revision。
若误启动，请退出应用并再次执行上述恢复流程。
