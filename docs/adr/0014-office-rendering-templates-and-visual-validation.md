# v1.1 Office 使用本地确定性渲染、受控模板和只读视觉验证闭环

状态：已接受，随 v1.1 实施

v1.1 保留现有安全快速预览作为“结构近似预览”，并增加受控本地 renderer provider，将 Office 文档渲染
为 PDF 和逐页/逐幻灯片 PNG。只有固定渲染器构建、字体包和渲染参数产生的结果才能称为“高保真预览”；
回退到浏览器 DOCX/XLSX/PPTX renderer 时必须显著标为近似，不能复用高保真验收标记。

GA 路径不得把文档上传到云端渲染。renderer 以独立受限进程运行，禁用网络和宏，使用只读输入与私有
临时目录，并受超时、内存、页数、像素数和产物大小预算约束。发布证据记录二进制 SHA-256、版本、许可、
字体清单和五个原生目标结果；缺少某个平台的可再现 renderer 时，该平台不得宣称高保真 Office GA。

v1.1 的信任链分为以下四层：

1. 构建时从版本一致的 `package.json`/`pyproject.toml`、干净 tracked checkout 和完整 Git commit
   生成 canonical `release-identity.json`，并将该字节的 SHA-256 作为生成模块收入
   PyInstaller PYZ/PKG。运行时只能从冻结应用的
   `sys._MEIPASS/app/data/release-identity.json` 读取，且必须与可执行包内摘要一致；不接受
   source tree、环境变量回退或单独替换数据资源。
2. renderer attestation v2 必须同时绑定该 `app_version`/`release_commit`、平台、renderer/字体身份、
   dependency/font/license manifest、执行文件以及除 attestation 自身外全部常规文件的规范化树
   （相对路径、mode、size 和 SHA-256）。符号链接、特殊文件、宽松权限及增删/替换漂移均拒绝。
3. 原生闭包校验直接解析 PE、Mach-O 和 ELF 的 64-bit 目标架构、import/load command 和搜索路径。
   非明确审批的系统库必须是签名 dependency manifest 中具有精确路径/大小/摘要的私有文件；
   假执行文件、错架构、未声明库或多余原生文件都 fail-closed。
4. 发布 staging 只接收一个明确 target 的 canonical lock，校验文件集、模式、摘要、二进制树和
   attestation/release identity，Python admission 还必须使用内置 Ed25519 trust root 验签。通过后逐文件
   复制到新建私有 PyInstaller 快照并二次校验，不将外部可变 stage 直接交给打包器。POSIX 发布 lock
   只接受 `0755` 目录及 `0644`/`0755` 文件，快照再收紧为 `0555`/`0444`。renderer 文件不作为
   PyInstaller Analysis 输入，避免真实 ELF/PE/Mach-O 被自动重分类、UPX、改写 load command 或重签；
   Analysis 完成后才以精确 `DATA` TOC 注入，并在注入后和 `COLLECT` 前交叉核对 datas/binaries、源 inode、
   mode、size、摘要、完整目录清单及目标平台文件系统别名碰撞。Windows 额外拒绝尾随点/空格、
   DOS 设备名、8.3 短名标记和非法/ADS 字符，macOS 按默认 APFS 的大小写与 Unicode 规范等价关系归并目标键；
   pre-v1.1 的空 renderer 根也使用原生目标语义拒绝 ambient 别名。
   CI 在 dependency install、frontend build、PyInstaller、Tauri build 和 artifact upload 边界核对
   HEAD/`GITHUB_SHA`、tracked clean、unsafe index flag、忽略/非忽略 source shadow；`frontend/out`、冻结后端
   和 Node runtime 另以路径、mode、mtime、size、内容及内部 symlink 目标组成的生成物 seal 贯穿后续阶段。
   五个目标的 PyInstaller、hooks、测试/审计工具及全部传递依赖使用独立 SHA-256 lock，安装前还核对 lock
   文件自身摘要。安装包生命周期 schema v3 绑定 release commit，并要求父 shell 在首次安装/挂载前封存
   installer/package 的 size 与 SHA-256；桌面启动前、生命周期结束后及上传前均重算，publish 下载后还必须
   与同一报告和 checksum 再次一致。
   macOS 必须先完成 renderer 内层签名再封存最终 bytes；后续 app 签名只验证而不改写该树。
   未受 lock 摘要约束的环境指向、ambient renderer、多目标混入或缺失 staging 不能生成
   v1.1 冻结后端。

renderer 实际执行的进程/环境边界已固定绝对执行文件和最小 `PATH`，为每次渲染创建私有
HOME/TEMP/XDG/profile/cache，只继承 Windows 必需的系统根，并通过私有 Fontconfig 配置只声明捆绑字体。
进程树取消、超时回收、输入/输出和资源预算也已进入运行合同。declarative
native-sandbox manifest 始终只输出 `declared-not-proven`；另一个签名 helper 行为协议会对本机
网络、宿主写入、私有输入/输出和延迟后代做对抗尝试，由 Python 外部观察并生成与
target/contract/bundle tree/manifests/launcher/helper/nonce 交叉绑定的 path-free 报告。启动、CLI 自检和
bundle gate 都要求该行为报告成功后才能继续 144-DPI golden probe 或 Office 写入；发布态不允许
跳过 smoke。

路径摘要和行为 nonce 仍不等于“已验证 inode/handle 就是实际执行映像”。同 UID 恶意进程理论上可在
最后一次摘要后、spawn 重新按路径打开前替换 launcher；PyInstaller 私有快照在 pre-`COLLECT` 复验与
打包器重新打开文件之间也有同类窄窗口。当前只读 mode、重复 inode/摘要检查、最终 bundle self-test、
artifact 上传后回验会拒绝持久漂移并阻止错误公开发布，但不能把同 UID 对手假设为已经消除。正式原生
组合必须提供句柄绑定执行/复制，或由不同所有者 ACL、Windows 私有 DACL、平台 code identity 等 OS 边界
证明实际 process image 和被消费字节；在这份证据进入同 tag 前，对应门禁继续关闭。

以上源码合同仍不是宿主 OS 证据：公开仓库没有 macOS XPC/App Sandbox、Windows AppContainer、
Linux namespace/seccomp/cgroup 的真实 launcher/helper 实现和已签名五目标报告；
CoreText/DirectWrite 也尚未证明不会回退宿主字体。在这些真实资产和证据进入同 tag 发布链前，
平台 sandbox 门禁仍是未满足。

preview 与 precommit 必须复用同一个 admission wrapper，v1.1 正式组合的 renderer 并发上限为一，
排队超时或取消不得泄漏 permit 或后台渲染任务。

预览缓存键至少包含文档 SHA-256、renderer/字体指纹和参数版本。编辑、restore 或 rewind 改变文档 SHA
后，旧缓存立即失效；界面不能把旧图像绑定到新版本。渲染失败不得覆盖源文件或生成伪成功预览。

## 高保真编辑与模板

“高保真编辑”表示只修改支持矩阵内的目标结构，并证明未触及的 OOXML part、relationship 和媒体摘要
保持不变；不表示任意 Office 文件均可无损编辑。遇到宏、OLE/ActiveX、外部连接、未知关系图或不受支持
的目标结构时，编辑必须在提交前 fail-closed，原文件保持不变。

第一方模板是 v1.1 GA：模板随应用签名清单分发，具有稳定模板 ID、版本、文件 SHA-256、格式、placeholder
schema、允许操作和预期渲染基线。生成时复制模板，不原地修改模板资产。用户模板导入是 Beta：导入时
检查 OOXML 安全特征、placeholder 合同、大小预算和独立重开/渲染；内容或 schema 改变后重新验证。

支持矩阵冻结在 v1.1 发布合同中。模板不能绕过该矩阵；缺失 placeholder、类型不符或目标关系不唯一时
必须拒绝，不能让模型猜测位置。模板、编辑结果、渲染产物和验证报告都关联同一 `root_turn_id` 和
checkpoint。

## 视觉验证闭环

Office 提交采用以下顺序：

```text
受控草稿 -> OOXML 重开/安全检查 -> 结构 delta 检查 -> 本地渲染
         -> 确定性视觉规则 -> 提交 seal 或最多两轮受控修复
```

预提交草稿没有 `finalized checkpoint`，因此不得为了让只读验证 Agent 访问它而提前
覆盖用户文件，也不得放宽 Agent 的 checkpoint 来源合同。直接工作区路径中，提交授权只来自
确定性草稿检查；Agent 只能在轮次结束、checkpoint 完成后补充只读证据，不能把失败
草稿改判为通过。

若某发布配置同时要求“Agent 必须在提交前阅读”和“失败后原文件从未可见变更”，必须改用
隔离晋级流程：先在 app-owned worktree/受控副本中提交并生成 finalized checkpoint，让 Agent
只读验证，通过后再以第二个 `WorkspaceMutationTransaction` 和精确 candidate seal 原子晋级到
用户工作区。当前工作区直接提交不能伪装成这种隔离流程。

### 事务接线约束

- precommit coordinator 只能接收 `WorkspaceMutationTransaction` 派生的不可变 view；view 同时绑定
  visible baseline、private staging、relative target、workspace inode、session/message/call、root turn、
  turn、checkpoint 和 workspace instance。调用方不能分别提供 baseline/staging 路径，把 A 的策略套到
  B 的 candidate。
- transaction 一旦为 Office precommit armed，普通 `commit()` 必须机械拒绝。validation session 只允许
  一次 capture/compare，只能一次性消费它自己返回的最新 result；旧 result、跨 session/transaction seal
  或手工重建的对象都不能授权提交。
- `edit` 的 baseline 是 transaction 建立时的可见文件摘要，candidate 只存在私有 staging；
  transaction 提交仍必须复核可见 baseline，防止并发编辑。
- 模板 `create` 要声称与预期视觉一致，必须有签名模板/golden policy。普通无模板 `create` 使用代码拥有、
  绑定 renderer/字体/参数指纹的 standalone policy，只能证明独立重开、authoritative render、运行时身份、
  页完整性和绝对空白异常等检查通过，不能声称匹配一个不存在的视觉 golden。
- `edit` 的允许变化必须由服务端在 writer 完成后根据受信操作摘要归一化，不能从工具请求反序列化。
  PPTX 添加幻灯片使用精确 page delta，XLSX 工作表增删使用精确逻辑单元 delta；DOCX/XLSX 的页数和
  视觉变化上限也由实际操作收紧，调用方不能放宽。
- golden policy 同时绑定模板 ID/版本/manifest 摘要、模板 SHA-256、renderer/字体/参数指纹、可改 OOXML parts、可改
  页面区域和阈值；模型或单次请求不得放宽。
- authoritative renderer 且所有确定性检查通过时才能产生提交 seal；approximate renderer
  最多返回 `needs_review`，不得复用 authoritative pass。
- 修复只能改 staging，每次都必须重新 capture/render/compare；第二轮后仍非 pass 则 abort
  transaction。崩溃时 staging 不可见，不需要通过 rewind 恢复原文件。
- Repair Agent 只能执行无工具、JSON-only completion，使用 hash-locked 服务端 prompt，并在同一
  ProviderRegistry 内共享单并发 admission。每次调用受 Goal 执行状态、剩余 token/cost 预算、首 chunk
  admission、总超时和取消约束；success/failure/timeout/cancel receipt 只记录模型身份与 usage，不记录
  Office 内容或路径，usage 以幂等 source key 进入 Goal ledger 并参与重启恢复。传给模型的路径使用
  单轮 token，返回 JSON 仍按不可信输入处理；repair 只能改动已审核的 style/layout 投影，不能扩展原始
  语义 mutation。
- 验证结果的 candidate seal 至少包含相对路径、SHA-256、mode、size 以及 renderer/字体/
  参数指纹。transaction 必须在可见 rename/exchange 之前对已复制的隐藏 replacement 再次校验该
  seal；只在验证函数返回后紧接调用 `commit()` 仍存在 TOCTOU，不足以验收。
- compare 必须直接返回 report/candidate 绑定结果；未经验证的 draft 不能单独导出
  commit seal。seal 是进程内服务端证据，不是自证的签名 token，API 请求和模型输出不得
  反序列化成提交授权。
- 服务端只能通过 `WorkspaceMutationTransaction.commit_with_precommit_office_seal()`
  消费该证据。v1.1 的 sealed 路径限定为 `prepare_paths()` 的单文件 write，不得夹带
  delete、无关目录变化或第二个未封印输出。对 create，transaction 可以临时创建且只创建
  sealed relative path 上 baseline 缺失的祖先链；该链的集合和 mode 绑定到 staging
  快照，记入同一 journal，失败时在清理隐藏 replacement 后逆序回滚。任何额外/同级
  目录、既有目录 metadata 变化仍拒绝。seal 不进入 tool schema、checkpoint journal 或公开
  commit metadata。
- sparse staging、私有 writer 和最终 commit 运行在线程时，协程取消必须先等待对应 worker 得到确定
  结果，之后才能 abort 或传播取消；不得让后台 rename/exchange 与 `abort()` 并发。

当前源码已包含 production policy resolver、Repair Agent 构造和 authoritative runtime composition，
但浏览预览 provider 仍明确是 `approximate`，不会注册 authoritative precommit coordinator。仓库也没有
受正式发布签名的私有 renderer/CJK 字体部署，全部 v1.1 release gate 仍关闭。因此当前普通运行不会获得
高保真提交权限；只有 frozen release identity 与 attestation v2 一致、原生依赖闭包通过、签名
模板/golden 和冻结字体齐备，且真实 144-DPI probe、对应平台 OS sandbox 与原生矩阵证据都进入
受审发布组合后才能安装 coordinator。缺少任一项时写入 fail-closed，不能回退普通 `commit()`。

确定性验证先于模型判断，包括页/幻灯片/工作表数量、文本和公式存在性、溢出/裁切信号、空白异常、
渲染完整性，以及基于用例 manifest 的 before/after 允许变化区域。每个 golden case 固定 renderer/字体
指纹、尺寸、忽略掩码和可接受阈值；更换 renderer 或字体必须重新建立并人工批准基线，不能直接沿用旧
分数。

冻结 renderer 还必须通过真实 144-DPI 执行 probe：使用签名 bundle tree 内的固定 DOCX 和 canonical
probe manifest，调用与生产相同的 authoritative provider 实际生成 PDF 和 PNG，校验页序、尺寸、
canonical RGBA pixel SHA-256、PDF 摘要与嵌入字体数，并在执行前后重新验证 bundle tree。报告只包含
摘要和计数，不暴露路径、文本或字体名。当前源码已有该合同和对抗校验，但仓库中没有真实私有
renderer/probe 资产，也没有签名候选包上的成功 probe 或五目标矩阵证据，因此不能声称高保真 GA。

验证 Agent 运行在独立只读子会话，只能读取规范化结构、低敏缩略图、视觉差异和任务意图，输出版本化
`pass`、`fail` 或 `needs_review` 及证据坐标。它没有文件写入、权限批准、checkpoint 完成或自我验收能力。
确定性检查失败时模型不能判定通过；两轮修复后仍失败则保留原文件并请求用户处理。

## 验收影响

GA 前必须使用包含 CJK 字体、恶意/不支持输入及三种格式复杂样例的固定 corpus，在五个原生目标生成
结构报告、逐页图像、差异 manifest 和 Microsoft Office/WPS/LibreOffice 抽验记录。所有支持样例必须
独立重开；所有不支持样例必须原样拒绝；未触及 part 摘要必须相同；rewind 后文档摘要、预览缓存和界面
版本必须一致。每个目标还必须证明原生依赖闭包、对应 OS sandbox 真实拒绝网络/宿主写入并回收
后代进程，以及 144-DPI 签名 probe 与发布 identity 完全匹配。只做浏览器截图、只让模型“看起来
正确”、只验证 metadata/manifest 或只证明文件能打开，都不足以验收。
