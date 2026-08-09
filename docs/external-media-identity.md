# 外部媒体身份

AniMemo 的 External Media Identity 为手账条目提供持久、Provider 无关的外部资料标识。Phase A 交付 Persistent External Media Identity，Phase B 交付 Metadata Refresh，Phase C 在不改变该边界的前提下增加账号连接与显式收藏导入。

## 架构

`ExternalMediaIdentity` 从属于 `JournalEntry`，每个条目对同一 Provider 最多绑定一个身份。同一用户不能把同一 Provider 的同一外部 ID 绑定到两个条目；不同用户可以独立绑定相同条目。

模型只保存通用字段：`provider`、`external_id`、`canonical_url`、规范化 metadata snapshot 及抓取时间。`JournalEntry` 不增加 Bangumi 专用字段。新 Provider 通过 `journal.external_media.registry` 注册，并实现统一的搜索、详情抓取、规范化、规范 URL 和刷新接口。

当前 Provider：

- `bangumi`：使用固定 HTTPS API 地址。
- 搜索优先调用实验性 `POST /v0/search/subjects`，失败时回退 legacy 搜索。
- 详情同时读取 `/v0/subjects/{id}` 与 `/v0/subjects/{id}/persons`，人物接口不可用时从 infobox 提取制作公司。
- 搜索缓存 300 秒，详情缓存 900 秒；用户主动刷新绕过详情缓存。
- 请求超时为连接 4 秒、读取 8 秒。

实现依据为 [Bangumi API 文档](https://bangumi.github.io/api/)、[官方 OpenAPI](https://github.com/bangumi/server/blob/master/openapi/v0.yaml) 与 [User-Agent 约定](https://github.com/bangumi/api/blob/master/docs-raw/user%20agent.md)。

## 生命周期

创建手账时可以附带：

```json
{
  "external_identity": {
    "provider": "bangumi",
    "external_id": "123456"
  }
}
```

服务端先从 Provider 读取并规范化资料，再在一个数据库事务中创建条目与身份。已有条目使用以下 Provider 无关接口：

- `GET /api/entries/{entry_id}/external-identities/`
- `POST /api/entries/{entry_id}/external-identities/`
- `DELETE /api/entries/{entry_id}/external-identities/{provider}/`
- `POST /api/entries/{entry_id}/external-identities/{provider}/refresh/`

所有身份接口都要求登录，并通过手账条目所有权隔离数据。跨用户访问返回 404。绑定事务锁定用户行，保证同一用户的重复外部 ID 在并发请求下只能成功一次。

解绑只删除身份，不删除手账、评分、评论、标签或观看记录。

## Metadata Snapshot

Snapshot 只保存界面与刷新所需的规范化摘要，不保存 Provider 的巨大原始响应。Bangumi snapshot 包含标题、日文名、简介、话数、首播日期、制作公司、标签、站点评分、海报、Provider 名称与规范 URL。

`provider_updated_at` 是 Provider 无关的可选字段。Bangumi subject 响应没有可靠的更新时间，因此当前保持为空，不伪造时间。

## 刷新策略

手动刷新始终更新 snapshot，并只允许覆盖以下 Provider-owned 字段：

- `japanese_title`
- `airing_period`
- `studio`
- `episodes`
- `poster_url`

用户上传海报或受信任自定义海报继续拥有显示优先级。以下 User-owned 字段永不由 Provider 刷新覆盖：`title`、`description`、`tags`、`personal_score`、`watch_status`、`review`、`visibility`、`custom_poster_url`、`poster_file`、`share_slug`。

刷新响应返回更新后的 `identity`、`metadata`、`applied_fields` 和 `changed_fields`，前端不展示原始 JSON。

## 安全与可观测性

- 外部 ID 必须是正整数字符串，并在请求前规范化。
- Provider 仅访问代码内固定端点，不接受任意 URL，也不使用 Bangumi 用户令牌。
- 海报地址经过受信任 HTTPS host 校验。
- 错误使用稳定代码，覆盖非法 ID、不支持的 Provider、404、超时、不可用、无效响应与绑定冲突。
- 外部请求日志仅记录 Provider、端点类型、状态或错误类别，不记录 Cookie 或 Authorization。
- `BANGUMI_USER_AGENT` 默认是 `AniMemo/1.0 (+https://re-anime.cc)`；部署者应按实际站点维护可联系信息。
- `BANGUMI_IMAGE_PROXY_BASE_URL` 控制 Bangumi 图片代理前缀；留空时使用固定 HTTPS 图片源。

## 阶段边界

- Phase A：持久化外部媒体身份。
- Phase B：手动刷新 Provider-owned metadata。
- Phase C：用户外部账号授权与只读收藏导入。
- Phase D：显式、可审计的双向同步策略。

Phase C 使用用户范围、凭据加密的 `UserExternalAccountConnection`，不复用服务端 Integration Gateway 的 `IntegrationConnection` 信任边界。收藏导入只通过显式 Preview/Apply 创建或绑定 `ExternalMediaIdentity`，不会自动写回 Bangumi。

Phase D 实现前必须先定义字段级 source of truth、初始同步基线、冲突状态和人工解决流程。`JournalEntry.updated_at` 可能因海报或 metadata 刷新变化；Bangumi 收藏的 `updated_at` 也被官方 OpenAPI 标记为不可靠，因此两者都不能直接用作“最后写入者胜出”的同步依据。

新增 Provider 时复用同一身份模型和服务接口，禁止把 Provider 专用字段加入 `JournalEntry`。
