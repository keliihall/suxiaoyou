# 暂缓抽出 NativeDialog，先用遥测判断后端 fallback 是否仍有价值

前端已经实现 Tauri 优先的原生文件选择流程。桌面端下，`browseFiles()` 和 `browseDirectory()` 直接调用 `@tauri-apps/plugin-dialog` 的 `open()`；只有导入失败时才回退到 `POST /api/files/browse*`。

后端中的平台分发代码只在两类场景下运行：

- 桌面端 Tauri dialog 插件导入失败，这在生产中应当很少发生。
- 远程访问用户从浏览器触发文件选择，但这种情况下对话框会开在服务端机器上，用户看不到，逻辑上并不成立。

因此，当前不立即抽出 `NativeDialog` 模块。先为 `/files/browse` 和 `/files/browse-directory` 增加轻量遥测，观察一个版本周期。

后续判断：

- 如果 fallback 触发率接近零，删除 `/files/browse`、`/files/browse-directory` 及后端平台分发代码，同时删除前端 fallback 分支。
- 如果 fallback 触发率有意义，再按原计划抽出 `NativeDialog` 模块，提供 `pick_files()` 和 `pick_directory()`，并用私有 `_PlatformDialog` 协议承载 macOS、Windows、Linux 和测试实现。

## 已考虑方案

- **现在就抽出 NativeDialog**：会提前为可能没有生产调用者的代码支付维护成本。
- **现在直接删除 `/api/files/browse*`**：缺少真实数据，可能误删仍有人依赖的 fallback。
- **让后端通过 Tauri IPC 调用 plugin-dialog**：当前后端子进程没有可用返回通道，新增通道成本过高。桌面端原生对话框应继续由前端触发。
