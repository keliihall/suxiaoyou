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

## 适合场景

- 把多份资料整理成一份结构清晰的报告。
- 从会议录音稿、纪要或文档中提取重点和待办事项。
- 分析 Excel 表格中的数据变化、预算差异和业务线索。
- 根据 PPT 或方案材料生成讲稿、摘要和汇报邮件。
- 批量整理、重命名、归档本地办公文件。

## 本地优先

苏小有以电脑端工作为中心。文件、对话、生成结果和工作流程默认围绕本机展开。

当你使用本地模型或本地兼容接口时，材料处理可以在自己的电脑上完成。需要使用云端模型时，也由你自行配置可用的模型服务商和密钥。

## 安装与使用

> 发布状态：[`v0.9.0-rc.1`](https://github.com/keliihall/suxiaoyou/releases/tag/v0.9.0-rc.1) 是 `0.9.0` 的候选测试版，提供七个原生安装包，覆盖 Windows x64、macOS arm64/x64 与 Linux x64/ARM64。在取得 Apple Developer ID
> 和公证凭据前，macOS 只提供明确标注的候选测试包：DMG 本身未签名、未公证，内含的
> `.app` 仅使用 ad-hoc 临时签名，不作为面向普通用户的正式发布。
>
> `v0.9.0-rc.1` 的安全范围中，Bash/Python 命令执行只在安装了 Bubblewrap 的 Linux 开放；
> macOS 与 Windows 均 fail-closed 禁用，不会退回无隔离执行。

系统要求：macOS 11.0 或更高版本（Apple Silicon 与 Intel 分别提供原生安装包）；Windows x64、Linux x64 与 Linux ARM64 安装包由对应平台的原生 CI runner 构建与验证。

1. 下载并安装苏小有桌面端应用。
2. 在设置中选择可用的模型服务商、本地模型或自定义接口。
3. 新建对话，上传或选择要处理的办公材料。
4. 直接提出目标，例如“生成一份调研报告”“整理会议纪要”“分析这张表格”“写一封汇报邮件”。
5. 根据结果继续追问、修改或生成新的文件。

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
