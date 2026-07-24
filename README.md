# 苏小有

[![CI](https://github.com/keliihall/suxiaoyou/actions/workflows/ci.yml/badge.svg)](https://github.com/keliihall/suxiaoyou/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

<p align="center">
  <img src="Suxiaoyou-Logo/mascot.png" width="120" alt="苏小有 Logo" />
</p>

苏小有是安装在电脑端的 AI 工作助理，面向日常办公材料处理与知识工作。

它能读懂 Word、Excel、PPT 和本地资料，帮助你自动生成报告、纪要、分析、计划、邮件等工作成果。

苏小有的价值，是可以直接处理电脑里的办公材料，同时让文件、数据和工作流程尽量留在自己的电脑上，更安全，也更高效。

## 能做什么

- **读懂办公文件**：理解 Word 文档、Excel 表格、PPT 演示稿、PDF、本地文件夹和多份资料。
- **生成工作成果**：根据材料生成调研报告、会议纪要、工作计划、项目复盘、邮件草稿、表格分析和汇报提纲。
- **整理本地资料**：围绕本地文件进行归纳、改写、对比、提取重点和任务拆解。
- **辅助数据分析**：读取表格数据，发现异常、提炼结论、整理指标说明和行动建议。
- **延续工作上下文**：在同一条对话里继续补充、修改、追问和生成后续材料。
- **跨多轮推进目标**：在本地桌面对话中输入 `/目标 <要完成的结果>`，让助手在预算和现有权限内自主续跑，并可随时查看、编辑、安全暂停、继续或清除。

## 适合场景

- 把多份资料整理成一份结构清晰的报告。
- 从会议录音稿、纪要或文档中提取重点和待办事项。
- 分析 Excel 表格中的数据变化、预算差异和业务线索。
- 根据 PPT 或方案材料生成讲稿、摘要和汇报邮件。
- 批量读取、分类和汇总本地办公文件，并生成整理建议或受支持的输出文件。

## 本地优先

苏小有以电脑端工作为中心。文件、对话、生成结果和工作流程默认围绕本机展开。

当你使用本地模型或本地兼容接口时，材料处理可以在自己的电脑上完成。需要使用云端模型时，也由你自行配置可用的模型服务商和密钥。

## 安装与使用

> 发布状态：`v1.1.0` 以公开的 `UNSIGNED-DEGRADED` 预发布配置交付。七个安装包均在文件名中带有
> `UNSIGNED-DEGRADED`，Release 不设为 latest；Windows/Linux 包未做发行方签名，macOS 应用仅为
> ad-hoc 签名，DMG 未做 Developer ID 签名、Apple 公证或 staple。发布清单与能力声明会以机器可读
> 形式记录这些边界。
>
> v1.1 开放 ACP、worktree、checkpoint/rewind、Hooks 和只读验证 Agent，并增强 Office 模板、图表、
> 复杂格式及预览/视觉验证闭环。公开包不携带项目冻结的 authoritative Office renderer，因此权威
> Office authoring/视觉提交仍会 fail-closed；若本机存在兼容 LibreOffice，只可提供明确标为近似的预览。
>
> 声明式 `write`、`edit`、`apply_patch`、Office 与版本恢复在各桌面平台使用受守卫的目标文件
> 流程，并由对应原生 runner 做故障注入和冻结后端验证。Bash/Python 在 Linux 使用 Bubblewrap、
> 在 macOS 使用 Seatbelt，并先在应用私有事务副本执行；Windows 使用 Job Object 回收进程树，但命令
> 直接作用于已批准工作区，不提供文件系统或网络隔离，失败可能留下改动，因此界面和发布说明不会把
> Windows 命令描述为隔离或可回滚执行。
>
> checkpoint/rewind 使用耐重启的工作区身份 v2：POSIX 优先使用目录 xattr，
> 不支持时使用安全标记文件；Windows 使用原生卷序列号和文件 ID。旧 `stat-v1` 数据会在
> 启动时以“外部历史先校验、数据库后提交”的方式幂等迁移。目录缺失、被替换或身份不可证时，
> 旧 checkpoint 不会绑到同路径的新目录；只保留并阻断该工作区的恢复，不影响其他工作区或本地服务启动。

系统要求：macOS 11.0 或更高版本（Apple Silicon 与 Intel 分别提供原生安装包）；Windows x64、Linux x64 与 Linux ARM64 安装包由对应平台的原生 CI runner 构建与验证。

1. 下载并安装苏小有桌面端应用。
2. 在设置中选择可用的模型服务商、本地模型或自定义接口。
3. 新建对话，上传或选择要处理的办公材料。
4. 直接提出目标，例如“生成一份调研报告”“整理会议纪要”“分析这张表格”“写一封汇报邮件”。
5. 根据结果继续追问、修改或生成新的文件。

目标模式当前只面向本地桌面单客户端。Remote/移动端、多窗口实时同步和应用退出后的云端执行不在
发布范围；详细的命令、安全边界、恢复语义和验证状态见[目标模式说明](docs/goal-mode-development-plan.md)。

## 项目结构

```text
desktop-tauri/    桌面端外壳与系统集成
frontend/         应用界面、对话、设置与文件展示
backend/          助手服务、工具调用、文件处理与模型连接
```

## 本地开发

建议使用 Node.js 22、Python 3.12 和 Rust 1.96.1。

```bash
npm ci
npm --prefix frontend ci --legacy-peer-deps
npm --prefix desktop-tauri ci
python3.12 -m venv backend/venv
backend/venv/bin/python -m pip install --require-hashes -r backend/requirements.txt
backend/venv/bin/python -m pip install -e backend --no-deps
```

常用命令：

```bash
npm run dev:all
npm run build:frontend
node --test scripts/*.test.mjs
cd backend && venv/bin/pytest -q
```

请勿提交 `.env`、模型服务商密钥、会话令牌、数据库和构建产物。

## 开源信息

苏小有面向中文办公、桌面集成、安全和发布流程持续改进。公开源码历史从
`v0.7.3` 基线开始。

项目采用 [Apache License 2.0](LICENSE)。
第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
安全问题请按[SECURITY.md](SECURITY.md) 私下报告。
