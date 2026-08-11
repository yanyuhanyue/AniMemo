# Core Watch History

`WatchHistoryRecord` 是 AniMemo Core 领域模型，从属于 `JournalEntry`。观看记录是否存在、是否可读写与任何插件的安装状态无关。

## 领域边界

Core 负责规范化、校验、关系型持久化、CRUD、排序和幂等。标准写入 DTO 包含 `watched_on`、`watched_label`、`brush_number`、`brush_label`、`episode_start`、`episode_end`、`notes` 以及有界 `metadata`。

`semantic_key` 是观看日期、刷次标签和话数范围的规范 JSON 的 SHA-256；数据库约束 `UNIQUE(entry, semantic_key)` 使 SQLite 与 PostgreSQL 使用相同去重语义。`sequence` 保存用户可见顺序，禁止依赖数据库默认顺序。

Core API：

- `GET/POST/PUT /api/v1/entries/{entry_id}/watch-history/`
- `PATCH/DELETE /api/v1/entries/{entry_id}/watch-history/{record_id}/`

条目列表只返回 `watch_history_count` 与 `last_watched_on`，详情界面打开观看记录页签时才读取完整记录。

## Importer 边界

`watch-history-importer` 是可选的数据入口，不是 Core parser。TXT/文档解析、编码探测、`DATE_TAG_RE` / `BRUSH_TAG_RE` / `YEAR_TAG_RE`、来源分组、来源特定标题规范化、文档解释、`build_preview`、Bangumi 候选解析、批次状态以及 preview/resolve/confirm 工作流全部留在插件。

唯一正确的数据流是：

```text
source-specific document
-> watch-history-importer
-> normalized WatchHistory input DTO
-> Core WatchHistory service
-> WatchHistoryRecord
```

未来的其他 importer、Provider 同步和手动录入都写入同一 Core 领域。Core 不知道源 TXT 的布局或标签编码，也不会发展成通用文档解析器。

## 迁移与回滚

`journal.0004` 将 `watch-history-importer` 的历史 `PluginData(namespace="watch_history")` 严格迁移为关系行，保留顺序、备注和额外 metadata。无法安全规范化时迁移 fail closed；成功后删除原 canonical PluginData，不 read-through、dual-write 或 write-through。

Importer `0.4.2` 通过用户绑定的 `host.journal` 与 `host.watch_history` 调用 Core。持久化批次受单批大小、每用户行数和 7 天 retention 限制；批次行锁保证并发提交只执行一次，Core mutation 与批次状态在同一事务内提交，完成事件使用 robust on-commit 投递。`dataCompatibility.rollbackFloor=0.4.0` 阻止回滚到会重新写第二份 PluginData 的 `0.3.x`。
