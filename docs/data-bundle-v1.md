# AniMemo Data Bundle v1

Data Bundle v1 是用户手账的权威 JSON 备份与恢复格式：

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

以下数据永不导出：账号 access/refresh token、`credential_ciphertext`、OAuth state、`ExternalImportSession` 和任何已认证连接。上传封面二进制也不嵌入 JSON；bundle 只保存可移植的 URL 引用。

CSV 是独立的、有损便携导入格式，只接受 canonical snake_case 列。它不保证外部身份、观看记录、复杂 metadata 或上传文件往返，不能替代 Data Bundle 备份。
