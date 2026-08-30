# API 错误契约

AniMemo 只规范错误响应；成功响应继续保持各业务 endpoint 现有 payload，不额外包一层 `success`/`data`。

## Canonical shape

```json
{
  "code": "validation_error",
  "detail": "请求参数无效。",
  "correlation_id": "4d3a2b1c0f9e8d7c6b5a493827161504"
}
```

- `code`：稳定、可机器判断的 snake_case 字符串。前端逻辑必须根据 `code`，不要匹配中文文案。
- `detail`：由 `code` 白名单绑定的通用公开文案；业务代码不能传入自定义异常详情。
- `correlation_id`：服务器生成的 32 位小写十六进制关联编号，用于在受控内部诊断中定位同一次请求。客户端提供的同名字段或 header 不会被采用。

错误对象严格只有以上三个字段。字段校验详情、provider metadata、traceback、SQL、命令、绝对路径、SDK message、用户名和原始 stderr 均不属于公开合同。限流等待时间仅使用 HTTP `Retry-After` header，不放入 JSON 正文。

未知错误码或错误码与 HTTP status 不匹配时，服务端按 status 降级为已登记的通用错误码；无法分类时使用 `internal_error`。未知值绝不会原样进入响应。

HTTP status 仍然权威，错误不会被改成 200：400、401、403、404、409、413、429、503 和 507 各自保留语义。

## Stable codes

通用 code 包括：

| Code | HTTP | 用途 |
| --- | ---: | --- |
| `invalid_request` / `validation_error` | 400/422 | 请求结构或字段校验失败 |
| `authentication_required` | 401 | 缺少或已过期的认证 |
| `permission_denied` | 403 | 已认证但无权执行 |
| `not_found` | 404 | 资源不存在或对当前用户不可见 |
| `conflict` | 409 | 当前资源状态不允许操作 |
| `rate_limited` | 429 | 触发限流；等待时间仅通过 `Retry-After` header 返回 |
| `service_unavailable` | 502/503/504 | 安全服务或外部 provider 暂时不可用 |
| `storage_exhausted` | 507 | 媒体存储空间不足 |

认证流程还会使用已有的 `invalid_credentials`、`session_expired`、`session_revoked`、`two_factor_required` 和 `csrf_failed` 语义。外部账号、导入和同步继续保留已有 domain-specific code，例如 `external_identity_changed`、`sync_preview_stale`、`sync_context_changed`、`unsupported_import_schema` 和 `provider_unavailable`。

## Frontend handling

`src/lib/api.js` 的 `parseApiError(error)` 将响应归一化为 `{ code, detail, correlationId, status, retryAfterSeconds }`；`readableApiError` 只负责把它转成用户可读文案。`retryAfterSeconds` 仅从 `Retry-After` header 读取。

- 表单校验：根据稳定 `code` 和页面本地校验展示提示，不消费服务端异常详情。
- 提交、导入、同步等操作：使用页面自己的 toast/banner，并保留 domain code 的分支。
- 后台查询：显示上下文重试状态，不吞掉业务冲突。
- 401：共享一次 refresh 请求后重试原请求；403 不触发 refresh。

生产错误不包含 stack trace、SQL、文件系统路径、命令、SDK/provider message、用户名、凭据明文、OAuth secret 或远端 token。向支持人员报告问题时只提供 `correlation_id`。

## Authentication in OpenAPI

- access token：`Authorization: Bearer <token>`。
- refresh token：仅通过 HttpOnly Cookie；refresh endpoint 不接受 request body token。
- CSRF：写操作按当前 cookie/CSRF contract 发送 `X-CSRFToken`。

接口总览见 `/api/schema/`，交互式文档见 `/api/docs/`。
