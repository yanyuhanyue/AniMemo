# AniMemo Pre-GA Architecture

```text
AniMemo Core
|
+-- JournalEntry
+-- WatchHistoryRecord
+-- Analytics
|
+-- ExternalMediaIdentity
|   +-- explicit metadata source
|
+-- External Accounts
|   +-- provider adapters
|
+-- Plugin Platform
|   +-- watch-history-importer 0.4
|       +-- source parser, candidate resolution and import UX
|
+-- Integration Protocol v1
    +-- AstrBot Bridge v1
```

`accounts.User` 是 tenant identity。Core Journal、Watch History、Analytics 与外部媒体身份属于 `journal`；用户外部账号凭据通过独立 Provider registry 管理；Plugin Platform 只暴露窄、用户绑定的能力；Integration Gateway 以冻结的 HMAC、nonce/replay 与 identity binding 契约接入外部系统。

`watch-history-importer` 只解释特定来源文档并编排 preview/resolve/confirm，随后提交规范 DTO。它不拥有 canonical Watch History，Core 也不知道 TXT 格式。

插件包使用 CAS 内容身份、不可变 `slug + version` 与 generic rollback floor。Integration Protocol v1、AstrBot Bridge v1、Plugin Platform v3 安全边界以及已发布官方插件 artifact 不因本轮领域清理而改变。
