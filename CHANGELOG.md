# Changelog

## v0.7.3 — 2026-07-11

首个公开源码基线版本。新的公开仓库从本版本开始记录历史，不继承旧内部仓库的提交、分支、标签和旧版本号。

### 产品与桌面端

- 修复 Windows 窗口宽度与侧边栏断点不匹配造成的搜索、新建对话按钮不可见问题。
- 无项目对话的右键菜单支持在 macOS Finder 或 Windows 资源管理器中定位生成文件。
- 完善中文菜单、首次启动、远程访问、深色主题及会话时间显示。
- 前端改用跨平台系统字体栈，构建过程不再依赖 Google Fonts 网络访问。
- macOS Apple Silicon 与 Intel 分别使用原生构建，统一声明最低系统版本为 macOS 13.3。

### 文档与文件处理

- PDF 导出改为 ReportLab 实现，支持标题、段落、代码、嵌套列表、表格与中文内容，并防止隐藏 HTML 内容被意外导出。
- PDF 导出内嵌经过 OFL-1.1 合规重命名的 CJK 字体子集，不再依赖用户系统是否安装中文字体。
- 移除不允许再分发的 Anthropic `docx` / `pdf` / `pptx` / `xlsx` 技能实现，改为项目自有的 Apache-2.0 操作指南。
- 移除不具备再分发权的 PPTX 预览依赖。当前保留 PPTX 内容读取、原文件下载与外部应用打开能力，内置幻灯片预览暂不可用。

### 安全、开源与发布

- 恢复 Apache License 2.0、上游 `NOTICE`、安全报告说明及安装包第三方许可资源。
- JavaScript、Python 与 Rust 生产依赖提供版本化许可报告；安装后可在 `licenses/` 目录查看。
- 补齐 Anthropic Apache-2.0 skills/plugins、canvas 字体 OFL-1.1、shadcn/ui 和 macOS 可移植 Python 运行时的精确溯源与许可文本。
- Python 生产图改为带 SHA-256 的跨平台锁文件，CI 同时校验 Windows x64、macOS arm64/x64 和 Linux x64 wheel。
- macOS 后端固定使用 Python 3.12.13 可移植运行时，避免将仅兼容构建机新系统的 Homebrew 二进制带入安装包。
- 移除旧 PDF 依赖引入的 LGPL 运行时链；发布门禁会拒绝旧组件或不应随包分发的组件。
- 本地系统打开文件接口按凭据来源授权，远程访问令牌即使经本机回环隧道接入也不能启动本机应用。
- 加强 GitHub Actions 最小权限、固定 Action SHA、哈希依赖安装、分层签名和安装包完整性验证。

### 已知限制

- 在没有 Apple Developer ID 和公证凭据时，macOS 只能生成完整 ad-hoc 签名、明确标记的测试包；它不是 Developer ID 签名且未公证，不作为正式安装包发布。
- 跨平台安装包由 GitHub Actions 原生 runner 构建；本地 Apple Silicon 机器只产出 arm64 macOS 测试包。
- WhatsApp 通道仍在开发中，v0.7.3 不公开该通道，且安装包明确不包含未完成的 Node.js bridge 源码或运行时依赖。
- 消息渠道的 SDK、凭据表单和登录流程尚未完成端到端验证，v0.7.3 暂不开放消息渠道设置，也不把这些动态渠道模块打入安装包。
