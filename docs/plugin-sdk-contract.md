# Plugin SDK Contract

当前 SDK API 版本为 `2`。插件包仍由不可变 `slug + version`、CAS blob 与官方包内容身份共同标识。

## Manifest Declaration

Backend runtime 如需访问 Core 数据，必须在 `manifest.json` 声明：

```json
{
  "runtimes": ["backend"],
  "coreCapabilities": ["journal", "watch_history"]
}
```

可用 Core capability：`journal`、`watch_history`、`analytics`。能力只能由 backend runtime 声明；未声明的能力由 Host 返回稳定的 `capability_not_declared` 错误。

`settings` 与 `storage` 是独立 extension。没有对应声明时，`host.system_settings`、`host.user_settings(user)` 与 `host.storage(...)` 均被 Host 拒绝。

## Actor and DTO Rules

- `host.*.bind(actor)` 只接受已认证、已安装且启用该插件的当前用户上下文。
- Core capability 不接受任意 `user_id`，每次调用都重新确认 enabled installation。
- Journal、Watch History、Analytics 返回 DTO 或统计快照，不返回 Django ORM 对象。
- `HostCapabilityError`、`PluginStorageLimitError` 从 `plugin_host.sdk` 公开导出；官方插件不依赖 runtime/storage 内部模块。

## Versioning and Compatibility

新增或改变包内代码必须递增插件 SemVer；已发布的 `slug + version` 内容不可覆盖。Core capability 字段是向后兼容的可选 Manifest 字段，旧插件不声明时对应 Host surface 保持拒绝，不会隐式获得 Core 数据能力。

SDK v2 Core capability 的引入没有新增数据库模型。公共目录迁移另有专用排序 collation migration，见下述目录合同。Integration Protocol v1 wire contract 保持冻结。

## Frontend public catalogue reads

Frontend `host.api` forwards Core paths through the current user transport. New
public catalogue callers use `public/homepage/entries/`,
`public/showcase/{public_slug}/entries/` and `public/showcases/`, with separate
summary, facet, detail and field requests described in the
[public catalogue contract](public-catalog-contract.md). Process one bounded
page at a time and repeat the original query with `next_cursor`; check field
completeness before treating a preview as its original value.

The catalogue release migration retires `homepage/`, `showcase/{public_slug}/`
and `showcases/` with a strict 410 error on both Core prefixes. External callers
must migrate their paths and response handling. Expired cursors restart the
read; changed field revisions require discarding earlier fragments. The SDK's
generic transport remains the same, and does not imply that unknown external
plugins have already migrated. These HTTP reads do not change backend Host
capabilities or the frozen Integration v1 protocol.
