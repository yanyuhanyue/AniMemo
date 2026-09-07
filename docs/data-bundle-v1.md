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
| 媒体 | 远程封面、自定义封面、百科 URL 最多 1000 字符；封面沿用可信来源规则。恢复不下载媒体，也不读取或嵌入本地封面文件。 |

bundle、item、entry、external identity 和 watch history 的顶层未知字段均拒绝；扩展数据应放在已有 `metadata` 内。owner 由当前认证用户决定，不能由数据包指定。导入保留领域字段、关系和顺序；本地 ID、share slug、创建/更新时间与 `exported_at` 可重新生成。

外部身份快照保留 provider 已接受的完整内容。例如当前 Bangumi normalizer 接受 5000 字符简介和两个 500 字符标题；Unicode 内容在 ASCII 转义后可能超过 64 KiB，而完整 UTF-8 数据包仍只有数十 KiB。恢复使用与整体传输一致的 UTF-8 预算，不再以单个快照的 ASCII 转义大小拒绝这种合法值。合并多个快照后仍需通过完整 bundle 的总预算及全部嵌套校验。

## 大数据包的当前缺口

导出仍包含当前用户全部未删除条目，不做截断。现有普通写入允许的数据可能形成超过 2 MiB 的合法导出，当前同步导入无法恢复该数据包；因此完整范围的往返能力尚未闭合。一个合法的 800000 字中文短评即可复现这一缺口。

不能通过删原手账、拆文件连续导入、筛掉字段或提高固定条数冒充解决。后续恢复会话、分块传输和原子发布需要独立合同批准，具体兼容方案见 [大数据包恢复方案](proposals/large-data-bundle-restore.md)。该方案尚未实现，也未改变 v1 格式或数据库合同。

以下数据永不导出：账号 access/refresh token、`credential_ciphertext`、OAuth state、`ExternalImportSession` 和任何已认证连接。上传封面二进制也不嵌入 JSON；bundle 只保存可移植的 URL 引用。

CSV 是独立的、有损便携导入格式，只接受 canonical snake_case 列。它不保证外部身份、观看记录、复杂 metadata 或上传文件往返，不能替代 Data Bundle 备份。
