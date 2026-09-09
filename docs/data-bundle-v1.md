# AniMemo Data Bundle v1

Data Bundle v1 是用户手账的权威 JSON Portable Export / Import 格式；它不承诺重建完整 AniMemo Instance，也不是 Backup、Restore 或 Migration Bundle：

```json
{
  "format": "animemo-data-bundle",
  "schema_version": 1,
  "exported_at": "2026-08-09T00:00:00+00:00",
  "entries": []
}
```

每个元素包含 `entry`、`external_identities` 与 `watch_history`。条目保存标签、颜色、评分、状态、描述、远程/自定义海报引用和可见性；外部身份保存规范 ID、metadata v1 snapshot 与明确 metadata source；观看记录使用 Core DTO，数组顺序即 Core `sequence`。

导入先验证完整 bundle，再在单个事务中恢复。v1 恢复要求目标用户手账为空，避免标题猜测、覆盖和部分合并产生不确定结果。没有 `format` / `schema_version` 的旧 JSON 以稳定错误 `unsupported_import_schema` 拒绝，不推断旧别名。

## 同步恢复边界

HTTP JSON、JSON 文件和直接调用的 preview/import service 共用完整预检。HTTP 原始文件/请求体以及补齐默认值、规范化观看记录后的紧凑 UTF-8 JSON，均不得超过 `IMPORT_FILE_MAX_BYTES`（默认 2 MiB）。直接 service 以紧凑 UTF-8 JSON 计算输入大小。预算在任何条目或关联写入之前检查；输入格式错误、非有限数字和无法解析的深度均拒绝。JSON 文件不适用 CSV 的单行长度规则。

`entries` 不再另设 500 条限制；0、1、500、501 和上千条均可在上述预算内预览、完整恢复。单条、标签/颜色数组、身份数组和总条目数组都共享该字节预算，补齐默认字段后超限也拒绝，不靠省略字段扩大容量。

| 内容 | 边界与恢复语义 |
| --- | --- |
| 条目文字 | 与普通写入使用同一模型字段长度、评分、状态、可见性和海报安全验证。标题 200、日文标题 200、放送时间 50、制作公司 120、集数 30；描述和短评受总字节预算约束。恢复保留已存文字的边界空白。 |
| 标签 | 字符串数组；与普通写入共用类型校验，取消普通写入的静默 30 项截断。恢复保留已有数组的顺序、重复项、空字符串、空白和长标签。普通编辑仍整理空白并去重。 |
| 标签颜色 | 普通写入校验 JSON 对象，恢复共用校验并兼容历史上 Core 已接受的空 JSON 值与键值对数组，原样保存。对象内保留既有 JSON 值；不额外收紧为 30 项或 20 字符。复杂颜色值与其他条目数据共同受总字节预算约束；Core 无法读取的非对象在写入前拒绝。 |
| 外部身份 | provider 50 字符、external_id 200 字符、规范 URL 1000 字符；metadata 为有效 JSON 对象，与所有其他字段共同受完整 bundle 的 UTF-8 字节预算约束；schema version 为 1–32767。每条目同 provider 唯一、最多一个 metadata source，同一目标用户的 provider/external_id 唯一。 |
| 观看记录 | 每条目最多 500 条；刷次/话数为 1–32767；日期标签 80 字符、刷次标签 20 字符；备注最多 20 条、每条 500 字符；metadata 最多 4096 字节。使用 Core 的内容等价与冲突规则，等价重试归并，不等价记忆在写入前拒绝。 |
| 媒体 | 远程封面、自定义封面、百科 URL 最多 1000 字符；封面沿用可信来源规则。导入不下载 URL 或把图片字节嵌入包。当前实例、同 owner 的可识别托管 URL 经统一持有边界验证已有 LOCAL 字节、建立关系并计入逻辑额度；其他链接保持 link-only。 |

托管 URL 的 owner 来自当前认证或明确服务身份，包内不携带持有授权。`poster_file` 稳定引用、内部 UUID、服务器 key、路径和图片字节不加入 v1 schema。后期某条目因持有或额度准入失败时，同次导入的条目、关系和用量全部回滚。跨实例或跨 owner URL 不认领源对象；同步路径保留 2 MiB 预算与空手账要求，大包使用下述恢复会话。

bundle、item、entry、external identity 和 watch history 的顶层未知字段均拒绝；扩展数据应放在已有 `metadata` 内。owner 由当前认证用户决定，不能由数据包指定。导入保留领域字段、关系和顺序；本地 ID、share slug、创建/更新时间与 `exported_at` 可重新生成。

外部身份快照保留 provider 已接受的完整内容。例如当前 Bangumi normalizer 接受 5000 字符简介和两个 500 字符标题；Unicode 内容在 ASCII 转义后可能超过 64 KiB，而完整 UTF-8 数据包仍只有数十 KiB。恢复使用与整体传输一致的 UTF-8 预算，不再以单个快照的 ASCII 转义大小拒绝这种合法值。合并多个快照后仍需通过完整 bundle 的总预算及全部嵌套校验。

## 分块恢复会话

产品导入页的JSON文件使用专用恢复会话。文件先用`File.slice`的有界缓冲计算SHA-256，再以不超过1 MiB的块顺序上传；刷新后重新选择同一原文件、核对摘要，可从服务端已确认偏移继续。CSV保留原同步路径；演示模式的本地导入保持明确的2 MiB限制。v1格式和完整导出保持不变。

各路由均要求认证owner，UUID、摘要与偏移不提供授权。`/api/v1/bundle-restores/`创建会话，`current/`读取当前未终结会话，`<uuid>/`读取状态/预览/收据；`chunks/`上传原字节，`validate/`全量预检，`commit/`原子恢复，`cancel/`取消。`/api/`别名具有同一合同，字段见[API v1合同](api-v1-contract.md#portable-bundle-restore-sessions)。

正常状态为`receiving → validating → ready → committing → completed`，其它终态为`cancelled/failed/expired`。操作绑定generation，迟到操作不能覆盖新状态。相同偏移与字节可重试，不同内容、缺块或越界拒绝。预检重算原文摘要，逐项保留完整领域值与关系，并以私有磁盘索引检查跨条目的重复身份；内存只需处理一个受界条目。

commit按owner→session→业务行锁序重新检查空目标、摘要和预检版本，领域行、history/identity、completed与收据同事务提交。正常API、CSV、provider和SDK创建共享owner提交边界。失败与外层回滚不留下半份手账，事件只在提交后发布。提交响应丢失时查询原会话；completed重试只返回原收据。取消不能撤销已完成业务。

预览最多50项并明确`items_truncated`；状态/预览/收据实际紧凑JSON不超过64 KiB，错误遵守现行三字段合同。预览不替代完整恢复后的导出字段比对。

### 操作预算

1 MiB只是块大小，2 MiB只是原同步路径预算，均不是字段或文件格式上限。默认操作配置如下；参考profile的观测范围与这些准入值分别记录。

| 配置 | 初值 |
|---|---:|
| BUNDLE_RESTORE_RAW_BYTES | 268435456 |
| BUNDLE_RESTORE_NORMALIZED_BYTES | 536870912 |
| BUNDLE_RESTORE_SINGLE_BYTES | 134217728 |
| BUNDLE_RESTORE_INDEX_BYTES | 33554432 |
| BUNDLE_RESTORE_DISK_BYTES | 2147483648 |
| BUNDLE_RESTORE_OPERATION_SECONDS | 300 |
| BUNDLE_RESTORE_HEADER_BYTES | 4096 |
| BUNDLE_RESTORE_MAX_DEPTH | 128 |

原始与规范化单条分别受SINGLE预算，根封装默认受4 KiB预算，解析默认拒绝深于128层的输入；这些操作参数均可显式配置。INDEX预算至少8192字节，实际索引文件也不得超过该会话预算。容量拒绝使用`bundle_restore_capacity`，不会把部分上传当成功。所有嵌套JSON键和值在预检中拒绝数据库无法保存的NUL与孤立Unicode代理字符；数据库提交错误先回滚业务事务，再记录failed与待清理占用。

3/16/64 MiB是样本；参考规模之外和较大恢复配置仍需适用验证，不能据样本宣告所有合法导出的容量合同闭合。[原提案](proposals/large-data-bundle-restore.md)保留历史设计背景，本节描述当前实现路径。

2026-09-08 Windows11/i9-14900HX/32GiB/PG16.15参考机已独立验证3/16/64MiB混合Unicode/转义输入，以及两个owner同时16MiB操作；冻结门为validate≤60s、commit≤30s、恢复进程RSS≤1GiB。更大配置的257MiB校准后，以另一份258MiB、首条review实际UTF8超过129MiB的文件独立留出，完整值/顺序、同收据重试和暂存归零通过。该次validate约77s、commit43s，恢复RSS约2.43GiB，低于事先冻结3GiB门。上述数字是这台参考机的回归门，不是运行时内存强制限制或生产SLO；最终新main仍须重新绑定证明。

### 较大文件的受控维护路径

`python manage.py restore_data_bundle --owner-id <active-owner-id> --file <absolute-read-only-file-path> --idempotency-key <uuid>`通过同一恢复服务完成分块上传和完整预检，默认停在ready。使用相同参数追加`--commit`后原子提交；重跑相同文件及幂等键可续传或返回原completed收据。命令始终只读用户原文件，拒绝非普通文件和读取期间改变的文件。

此命令要求已有的本实例维护权限，owner由显式active账号ID指定，空目标、媒体准入、事务、generation与收据仍由公共领域服务检查。运维可给这一个进程传入经本机校准的`BUNDLE_RESTORE_*`环境参数，提供较大操作预算；调用时须使用该版本的API环境、数据库与固定私有暂存挂载。文件通过独立只读挂载交给受控维护容器。不要直接修改生成的managed.env、签名发行材料或运行中的历史实例配置。较大配置验证结果属于对应参考profile，不继承默认样本的通过结论。

上述258MiB留出使用单个维护进程：RAW=536870912、NORMALIZED=1073741824、SINGLE=268435456、INDEX=67108864、DISK=4294967296、OPERATION_SECONDS=900、MAX_DEPTH=256，均使用`BUNDLE_RESTORE_`前缀。冻结验收门为validate≤180s、commit≤120s、完整adapter≤300s、恢复RSS≤3GiB。未声称512MiB上界、两个并发较大恢复或完整POSIX归档Restore已验证。默认HTTP专用恢复代理允许310s读等待，较大维护命令不依赖该代理。

### 私有暂存与维护

正式暂存根为实例Data Root的`bundle-restore-staging`，固定挂载至`/app/runtime/bundle-restore-staging`，不挂载到Web，也不进入Backup业务载荷。目录0700、文件0600，复用Windows DACL/owner检查；拒绝链接、重解析点、特殊文件和未知成员。

每owner至多一个未终结会话，实例至多两个活动validate/commit。DB短准入锁维护本功能的保守磁盘预留，实际原文/规范化/索引字节另行计量；预留与raw倍数不代表实测峰值。失败和待清理文件继续占用，实际字节清空后才释放预留。真实封面额度Q不受影响。

未完成且无活动操作的会话空闲24小时可过期，绝对寿命7天，终态收据保留7天。活动请求持有session行锁，cleanup跳过锁定操作；崩溃后的过期活动状态由维护命令收口。

受控运维入口在目标API容器中执行`python manage.py cleanup_bundle_restores --limit 25 --max-files 100`。每次最多检查25个候选，每会话最多删除100个已识别文件；结果包含inspected/cleaned/expired/retired/deferred。运维须在本实例维护计划中重复调用并检查deferred；本源码任务不安装宿主timer。该命令不读取或删除用户原文件。

数据库迁移为追加式`journal.0009_bundle_restore_sessions`，任意会话/收据仍存在时拒绝反向迁移。旧备份→新版本仅走已验证的窄布局转换与显式迁移；Restore在新目标API启动前运行`invalidate_restored_bundle_sessions`，只有固定暂存根确认为空时才失效复制来的未完成会话，并保留completed收据。该命令不用于普通升级或运行中的实例。新布局要求内部Updater至少1.0.2；宿主组件安装及新进程采用仍须受控执行与回读。

以下数据永不导出：账号 access/refresh token、`credential_ciphertext`、OAuth state、`ExternalImportSession` 和任何已认证连接。上传封面二进制也不嵌入 JSON；bundle 只保存可移植的 URL 引用。

CSV 是独立的、有损便携导入格式，只接受 canonical snake_case 列。它不保证外部身份、观看记录、复杂 metadata 或上传文件往返，不能替代 Data Bundle 备份。
