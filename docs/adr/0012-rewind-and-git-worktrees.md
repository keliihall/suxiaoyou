# v1.1 以 checkpoint 实现 rewind，并把 Git worktree 建模为工作区实例

状态：已接受，随 v1.1 实施

rewind 和 Git worktree 解决不同问题：rewind 恢复某个 turn 管理的本地状态，worktree 隔离 Git 仓库中的
并行任务。两者通过 `workspace_instance_id` 和 checkpoint 相连，但不能互相替代。普通 Office 文件夹、
非 Git 工作区和 Windows 受披露的直接执行路径不伪装成 worktree。

## Rewind 决策

rewind 采用预检、持久意图、幂等恢复和完成标记四阶段：

1. 锁定会话与工作区实例；要求生成已停止、Goal 已暂停、该 turn 的子 Agent 已停止。
2. 读取目标 checkpoint 之后的 ledger，比较每个当前路径的 SHA-256。当前内容不等于 ledger 的预期后态
   时报告冲突并保持所有文件不变，除非用户另行创建受审计的恢复副本；v1.1 不提供强制覆盖开关。
3. 写入 `rewinding` journal 后，逆序删除仍与提交后 SHA 相同的新建文件，并恢复被修改或删除的版本；
   多文件 Office 操作作为一个恢复单元处理。
4. 文件恢复完成后，在数据库事务中恢复对话前沿、待办和 Goal 引用，失效派生预览与索引，将涉及的
   checkpoint 标为 `rewound`，最后结束 journal。

任何步骤中断后，启动恢复必须幂等地完成或恢复到完整前态。相同 rewind 请求重复提交不得重复删除、
复制或生成新的语义结果。外部副作用在预检中显著列出并在结果中保留“不可逆”状态。

v1.1 GA 的 rewind 覆盖苏小有受控事务产生的创建、修改、删除、目录变化和 Office 多文件变更。任意
Bash/Python 在 Windows 直接写入、用户或第三方进程在事务外完成的变化不在可逆承诺内；检测到差异必须
冲突退出，不能借助旧快照静默覆盖。

## 工作区实例身份决策

canonical path 只表示位置，POSIX `st_dev`/`st_ino` 也不是跨重启的持久主键。工作区实例
改用身份 v2，并把“持久归属”与“单次操作的原生路径守卫”分开：

- POSIX 普通工作区使用随机 `marker-v2` token。目录 xattr 是首选表示；不支持 xattr 的
  文件系统使用通过 no-follow、create-only、`fsync` 和原子发布建立的
  `.suxiaoyou/workspace-identity-v2`。已存在的表示不会被重新生成，两种表示冲突时
  fail-closed。
- Windows 使用从目录句柄获得的卷序列号和 file ID，durable token 为 `winfile-v2`；
  不在工作区创建标记文件。
- 每次写入、恢复或 rewind 仍在打开目录后捕获当前原生 tuple，用于 dirfd/handle 绑定和
  TOCTOU 检查；POSIX tuple 不写成下次启动的连续性证据。

文件版本 manifest schema 3 与事务 journal schema 5 都持久化完整 durable token。旧 `stat-v1`
迁移必须先验证当前目录，再建立 token、复制并校验旧版本树，最后才提交数据库记录。
无法证明连续性时必须保留旧 token、journal 和 pin，不得因为路径相同而重绑。启动恢复按
工作区/journal 隔离故障，一个被阻断的实例不得使其他安全实例或后端就绪流程失败。

## Git worktree 决策

Git worktree 在 v1.1 作为 Beta 提供，只面向已验证的干净 Git 仓库。一个会话或子 Agent 绑定一个不可变
`workspace_instance_id`；工具解析路径时使用该实例，而不是重新读取项目的默认目录。

首版生命周期是 `create -> bind -> use -> detach -> remove -> gc`：

- 创建前验证仓库 common-dir、HEAD、目标引用、工作树状态和目标路径；不自动搬运未提交改动；
- 默认创建 detached worktree，只有显式分支操作才创建或切换分支；同一分支不能被多个实例占用；
- worktree 目录必须位于应用管理的位置并写入所有权元数据，不能删除未归属目录、脏 worktree 或存在
  活跃会话/checkpoint 引用的实例；
- 子 Agent 继承父 turn 的来源与权限，但获得独立工作区实例和 checkpoint；跨实例写入必须被路径策略
  拒绝；
- 清理只调用受控 Git 服务，不能直接递归删除路径；崩溃后通过 Git 元数据与应用所有权记录对账；
- rewind 只恢复实例内受控文件和对话状态，不执行 `git reset --hard`、不删除提交、不改写远程历史。

由应用创建的 Git worktree 还要求其管理根所在文件系统支持原生 xattr 身份；如果只能使用
标记文件回退，创建会在 `git worktree add` 前拒绝，以免应用内部身份文件使每个 checkout 变脏。

非 Git 工作区继续使用私有事务副本、文件版本和 checkpoint。worktree 不改变 Linux Bubblewrap、macOS
Seatbelt、Windows Job Object 与直接工作区执行的既有平台披露。

## 验收影响

rewind 必须通过创建/修改/删除、多文件 Office、冲突、重复请求和逐持久化边界崩溃注入。worktree 必须
通过 Git common-dir、符号链接/CJK 路径、并发分支占用、脏目录拒绝、活跃引用拒绝、崩溃对账和五个
原生目标的生命周期测试。没有真实 Git 仓库测试或只验证 `git worktree add` 命令，不足以退出 Beta。
