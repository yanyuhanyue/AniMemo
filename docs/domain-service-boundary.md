# Domain Service Boundary

本文件记录 Architecture Contract Hardening 在 `2026-08-11` 的领域边界。

## JournalEntry

`backend/journal/domain_services.py` 的 `JournalEntryService` 是 Core Journal 条目的共享 mutation/query boundary：

- 所有 list/get/update 查询都按 authenticated owner 与 `deleted_at IS NULL` 收口。
- DTO 只返回插件与 Web 共同需要的稳定字段，不暴露 ORM 实例或任意 `user_id`。
- create/update 统一使用 `JournalEntrySerializer`；插件字段由 Core allowlist 决定。
- create/update 统一派发 `journal.after_create` / `journal.after_update`，并携带 `source`。
- Web ViewSet 与 Plugin Journal capability 共享这条边界；传输层只负责认证、HTTP 与错误映射。

## Transaction Ownership

- Serializer 自己负责 JournalEntry 与外部身份/媒体引用的局部事务。
- `JournalEntryService.update_from_fields` 在 owner lock 下协调插件更新，再调用同一 mutation boundary。
- Watch History importer 负责更大的导入事务；它通过 Core Journal/Watch History capability 写入规范 DTO。
- open action hook 继续遵循 Host 既有 best-effort 失败策略；事件副作用不改变 Core 数据提交结果。

## Non-goals

本边界不改 Dashboard 查询、缓存、分页或 UI 状态，不新增 migration，也不把每一次 ORM 读取都包装成 service。External Media、Watch History、Analytics 等已有 service 继续拥有各自的领域规则。

## Verification

- `journal.test_domain_services` 覆盖 owner isolation、DTO、allowlist、shared mutation 与 hook source。
- `plugin_host.tests.test_capabilities` 覆盖插件安装状态和 capability actor binding。
- `plugin_host.tests.test_capability_contract` 覆盖未声明 capability、storage 与 settings extension 拒绝。
