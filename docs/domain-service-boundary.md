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

- Serializer、`JournalEntryService`、HTTP projection 与 Admin 的媒体外层事务使用 `atomic_media_mutation()`；业务字段、holder、逻辑用量同成同败，外层失败后的新上传由物理回执追踪清理。
- 涉及 `poster_file`、`custom_poster_url` 或 `clear_custom_poster` 的更新先取得服务端 owner 锁，再取得 entry 锁，并在锁内重验当前字段和用量。模型写入边界也执行同一准入，覆盖现有 Admin 与明确授权的服务写入。
- 默认 Core/Plugin DTO allowlist 不含 `poster_file` 或 `custom_poster_url`。Bundle/CSV 等实际允许媒体 URL 的入口明确传入允许字段和真实 owner，不能从请求体或导入数据选择 owner。
- 非媒体更新仍在 entry 锁下保持已有并发 PATCH 语义，不统一取得 owner 媒体锁。provider metadata 的 `poster_url` 不成为 holder；受保护的用户自定义封面不被自动 metadata 更新覆盖。
- 清理在提交后重验 holder、直接字段、其他图像角色与在途物理写入。只有最后一项有效保护释放后才可取得删除权。
- Watch History/import 等更大事务继续拥有全成全败边界。mutation events 提交后发布；open action hook 继续采用 Host 既有 best-effort 策略。

## Non-goals

本边界不改 Dashboard 查询、缓存、分页或 UI 状态；媒体 holder 的 additive schema 与迁移门见 [Media Storage Pool](media-storage.md)。它不把每一次 ORM 读取都包装成 service。External Media、Watch History、Analytics 等已有 service 继续拥有各自的领域规则。

## Verification

- `journal.test_domain_services` 覆盖 owner isolation、DTO、allowlist、shared mutation 与 hook source。
- `plugin_host.tests.test_capabilities` 覆盖插件安装状态和 capability actor binding。
- `plugin_host.tests.test_capability_contract` 覆盖未声明 capability、storage 与 settings extension 拒绝。
