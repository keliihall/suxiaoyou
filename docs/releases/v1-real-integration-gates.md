# v1.0 真实集成门禁

`scripts/v1-real-integration-gates.mjs` 把腾讯文档和 SiliconFlow 图片生成从“可选测试”升级为
可复核、默认拒绝的发布证据。普通 `pytest` 中的真实测试仍会在没有凭据时跳过，以保证开源 CI
不接触个人账号；门禁运行器则要求真实测试必须出现至少一个 `passed` 且 `skipped=0`，否则返回
非零状态。

门禁不会把 Token、API Key、测试文档 ID、基线文本或腾讯文档参数写入证据。日志落盘前会按
环境变量值再次脱敏，证据目录权限尽可能收紧为 `0700`，文件为 `0600`。发布验证还会校验日志
摘要、源码提交、发布 tag、工作区洁净度、运行器摘要和证据时效。

## 1. 无凭据合同测试

在候选提交上运行：

```bash
npm run test:v1-integration-contracts -- \
  --evidence-dir .codex-artifacts/v1-real-integrations
```

该命令执行两组不联网合同：

- 腾讯文档：官方端点与 raw `Authorization`、工具白名单、写工具审批、凭据脱敏、可逆写入在
  模拟模糊故障后的恢复；
- SiliconFlow：请求上限、PNG 完整性、临时 URL 安全、错误脱敏、持久化计费不确定状态、原子
  保存，以及同一调用不重复消耗 Provider 配额。

合同运行器允许在开发期的脏工作区生成诊断证据，但 RC/GA 验证会拒绝这类证据。正式证据必须
在已经打好相应 tag 的洁净候选提交上重新运行。开发期输出会明确显示
`TEST PASSED / EVIDENCE INELIGIBLE`，不会再用笼统的 `PASSED` 混淆测试结果和发布资格。tag
工作流使用 `--require-evidence-eligible true`，因此脏工作区、非 v1 tag 或运行中源码变化会直接
使发布 job 失败。

## 2. 腾讯文档真实账号闭环

必须使用专用测试账号和专用测试文档，不得使用个人或生产文档。测试夹具先包含一个稳定基线，
例如 `SUYO_E2E_READY`。参数对象必须采用当次官方 `tools/list` 返回的 schema，并且读、写、恢复
参数都要引用同一个测试文档 ID。

通过本地密钥管理器或 CI secret store 注入下列变量；不要把真实值写进脚本或 shell 历史：

| 变量 | 门禁要求 |
| --- | --- |
| `TENCENT_DOCS_E2E_TOKEN` | 专用测试账号的个人 Token |
| `TENCENT_DOCS_E2E_ALLOW_WRITE` | 必须精确等于 `I_UNDERSTAND_THIS_MODIFIES_A_DEDICATED_TEST_DOCUMENT` |
| `TENCENT_DOCS_E2E_TEST_DOCUMENT_ID` | 专用测试文档 ID |
| `TENCENT_DOCS_E2E_BASELINE_TEXT` | 写入前和恢复后都可读到的稳定基线 |
| `TENCENT_DOCS_E2E_READ_TOOL` | 可选，默认 `get_content`；必须是免审批读工具 |
| `TENCENT_DOCS_E2E_READ_ARGS_JSON` | 引用测试文档 ID 的 JSON 对象 |
| `TENCENT_DOCS_E2E_WRITE_TOOL` | 白名单内且要求审批的写工具 |
| `TENCENT_DOCS_E2E_WRITE_ARGS_JSON` | 同时引用文档 ID 和 `{{SUYO_TENCENT_DOCS_E2E_MARKER}}` |
| `TENCENT_DOCS_E2E_RESTORE_TOOL` | 白名单内且要求审批的恢复工具 |
| `TENCENT_DOCS_E2E_RESTORE_ARGS_JSON` | 同时引用文档 ID 和 `{{SUYO_TENCENT_DOCS_E2E_BASELINE}}` |

先做不联网预检，再执行一次真实闭环：

```bash
node scripts/v1-real-integration-gates.mjs preflight tencent-real-write
npm run test:v1-integration-live -- tencent-real-write \
  --evidence-dir .codex-artifacts/v1-real-integrations
```

真实闭环为：鉴权和工具发现 → 读取基线 → 写入随机标记 → 回读标记 → `finally` 恢复 → 回读基线。
写请求发生传输歧义时也会尝试恢复。如果恢复或最终回读失败，门禁失败，并且必须人工检查、恢复
专用夹具后才能再次运行；不得把失败测试遗留的文档当作下一次基线。

## 3. SiliconFlow 图片 Provider 闭环

图片门禁通过真实 `ImageGenerateTool`，而不只是直接调用 HTTP 客户端。一次获批请求必须完成：
凭据鉴权和可用配额 → 生成 → 下载并验证 PNG → 原子保存到工作区 → 写入 `completed` 账本 →
用相同 call id 本地重放且禁止构造第二个 Provider 客户端。

官方依据：[图片生成 API](https://docs.siliconflow.cn/en/api-reference/images/images-generations)
说明生成 URL 仅短时有效并列出 Kolors 参数边界；[实时价格页](https://siliconflow.cn/pricing) 当前将
`Kwai-Kolors/Kolors` 标为免费；[Rate Limits](https://docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation)
说明图片限额按账号的 IPM/IPD 计算并以 HTTP 429 拒绝超限请求。免费状态和配额都可能变化，因此
门禁仍保留成本确认、30 天价格复核与真实请求验证。

需要下列变量：

| 变量 | 门禁要求 |
| --- | --- |
| `SILICONFLOW_IMAGE_E2E_API_KEY` | 专用测试 Key |
| `SILICONFLOW_IMAGE_E2E_ALLOW_PAID_REQUEST` | 必须精确等于 `I_UNDERSTAND_THIS_MAY_USE_PROVIDER_QUOTA_OR_INCUR_COST` |
| `SILICONFLOW_IMAGE_E2E_MAX_REQUESTS` | 必须精确等于 `1` |
| `SILICONFLOW_IMAGE_E2E_MAX_COST_CNY` | 操作者接受的单次成本上限；不得低于代码内当前目录估价 |

运行：

```bash
node scripts/v1-real-integration-gates.mjs preflight siliconflow-image-real
npm run test:v1-integration-live -- siliconflow-image-real \
  --evidence-dir .codex-artifacts/v1-real-integrations
```

成本上限是发布操作保护，不等于 Provider 的强制消费限额；执行前仍需人工核对官方价格和账号
余额。真实请求成功本身是凭据与当前可用配额的证明。HTTP 401/403、402、429 会分别归类为凭据、
账单/配额、限流问题。超时或其他“供应商可能已接受请求”的结果归为计费不确定：先去 Provider
控制台核账，不得自动重试。运行器和工具都明确禁用自动重试，同一证据运行最多发出一次生成请求。
代码内目录价格的复核日期超过 30 天时预检会直接失败，必须先对照官方 HTTPS 价格页更新价格契约。

## 4. RC/GA 证据验证

RC 允许真实账号项在总计分卡中标记为 `pending_credentials`，但两组无凭据合同证据必须通过。
GA 必须同时拥有腾讯文档可逆写入和 SiliconFlow 工具闭环的真实通过证据。

在对应 tag 的洁净提交上执行：

```bash
npm run verify:v1-integration-evidence -- \
  --mode rc \
  --release-tag v1.0.0-rc.2 \
  --evidence-dir .codex-artifacts/v1-real-integrations

npm run verify:v1-integration-evidence -- \
  --mode ga \
  --release-tag v1.0.0 \
  --evidence-dir .codex-artifacts/v1-real-integrations
```

生成绑定每份 manifest/log SHA-256 的汇总文件：

```bash
node scripts/v1-real-integration-gates.mjs summarize \
  --mode rc \
  --release-tag v1.0.0-rc.2 \
  --evidence-dir .codex-artifacts/v1-real-integrations \
  --output INTEGRATION-CONTRACTS.json
```

`release.yml` 会在每个 `v1.0.0-rc.N` tag 上自动运行两项无凭据合同、强制证据资格、上传原始
manifest/log，并在 draft Release 附加 `INTEGRATION-CONTRACTS.json`。稳定 `v1.0.0` tag 会先
检查专用腾讯文档夹具与 SiliconFlow secrets，再执行两项真实门禁；缺凭据、跳过、恢复失败、配额
不足或计费不确定都会阻断工作流。稳定版汇总使用 `--mode ga`，不能降级为 `contract`。最终还需
通过计分卡伴随门禁：

```bash
npm run verify:v1-scorecard-integration-link -- \
  --mode ga \
  --scorecard path/to/v1.0-ga-scorecard.json \
  --summary path/to/INTEGRATION-EVIDENCE.json
```

默认只接受最近 168 小时的证据。需要更短窗口时可加 `--max-age-hours 24`；最大允许窗口为 744
小时。验证要求每个 gate 恰好一份证据，避免旧失败、重复重跑或不同提交的结果被拼接。正式归档时
应把整个证据目录与 RC/GA 计分卡一起保存，但不要提交包含账号诊断信息的日志到源码仓库。

## 仍需外部完成的事项

- 腾讯文档测试账号、个人 Token、专用可恢复夹具及当前官方工具参数 schema；
- SiliconFlow 专用 Key、官方价格复核、余额/配额和一次真实请求的成本授权；
- 在最终 `v1.0.0-rc.N` / `v1.0.0` tag 的洁净提交上重新采集证据；
- 腾讯恢复失败或图片计费不确定时的人工供应商控制台核验。

代码合同通过不能替代这些外部证据，也不得手工把 `pending_credentials` 改写为 `passed`。
