# API 错误契约

AniMemo 只规范错误响应；成功响应继续保持各业务 endpoint 现有 payload，不额外包一层 `success`/`data`。

## Canonical shape

```json
{
  "code": "validation_error",
  "detail": "请求参数无效。",
  "fields": {
    "title": ["该字段不能为空。"]
  }
}
```

- `code`：稳定、可机器判断的 snake_case 字符串。前端逻辑必须根据 `code`，不要匹配中文文案。
- `detail`：面向用户或调试的简短说明，保持中文可读性。
- `fields`：可选的字段级错误；没有字段归属时使用 `non_field_errors`。
- `metadata`：可选的非敏感附加信息，例如 provider 状态；不得放入 token、密文、SQL 或服务器路径。

HTTP status 仍然权威，错误不会被改成 200：400、401、403、404、409、413、429、503 和 507 各自保留语义。

## Stable codes

通用 code 包括：

| Code | HTTP | 用途 |
| --- | ---: | --- |
| `invalid_request` / `validation_error` | 400 | 请求结构或字段校验失败 |
| `authentication_required` | 401 | 缺少或已过期的认证 |
| `permission_denied` | 403 | 已认证但无权执行 |
| `not_found` | 404 | 资源不存在或对当前用户不可见 |
| `conflict` | 409 | 当前资源状态不允许操作 |
| `rate_limited` | 429 | 触发限流；可能带 `retry_after_seconds` |
| `service_unavailable` | 503 | 安全服务或外部 provider 暂时不可用 |
| `storage_exhausted` | 507 | 媒体存储空间不足 |

认证流程还会使用已有的 `invalid_credentials`、`session_expired`、`session_revoked`、`two_factor_required` 和 `csrf_failed` 语义。外部账号、导入和同步继续保留已有 domain-specific code，例如 `external_identity_changed`、`sync_preview_stale`、`sync_context_changed`、`unsupported_import_schema` 和 `provider_unavailable`。

## Frontend handling

`src/lib/api.js` 的 `parseApiError(error)` 将响应归一化为 `{ code, detail, fields, status, retryAfterSeconds }`；`readableApiError` 只负责把它转成用户可读文案。

- 表单校验：优先展示 `fields` 的 inline 错误。
- 提交、导入、同步等操作：使用页面自己的 toast/banner，并保留 domain code 的分支。
- 后台查询：显示上下文重试状态，不吞掉业务冲突。
- 401：共享一次 refresh 请求后重试原请求；403 不触发 refresh。

生产错误不包含 stack trace、SQL、文件系统路径、凭据明文、OAuth secret 或远端 token。

## Authentication in OpenAPI

- access token：`Authorization: Bearer <token>`。
- refresh token：仅通过 HttpOnly Cookie；refresh endpoint 不接受 request body token。
- CSRF：写操作按当前 cookie/CSRF contract 发送 `X-CSRFToken`。

接口总览见 `/api/schema/`，交互式文档见 `/api/docs/`。
