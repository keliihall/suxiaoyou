# Issue 管理

本仓库的 issue 和 PRD 使用 GitHub Issues 管理。执行 issue 操作时使用 `gh` CLI。

## 常用命令

- 创建 issue：`gh issue create --title "..." --body "..."`
- 查看 issue：`gh issue view <number> --comments`
- 列出 issue：`gh issue list --state open --json number,title,body,labels,comments`
- 评论 issue：`gh issue comment <number> --body "..."`
- 添加标签：`gh issue edit <number> --add-label "..."`
- 移除标签：`gh issue edit <number> --remove-label "..."`
- 关闭 issue：`gh issue close <number> --comment "..."`

在仓库内执行时，`gh` 会根据 `git remote -v` 推断目标仓库。

当技能说明中提到“发布到 issue tracker”时，创建 GitHub issue。

当技能说明中提到“获取相关 ticket”时，执行：

```bash
gh issue view <number> --comments
```
