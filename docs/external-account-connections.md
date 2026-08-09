# 用户外部账号连接

`UserExternalAccountConnection` 表示 AniMemo 用户与其自有外部账号之间的授权关系。它与 `integrations.IntegrationConnection` 完全分离：前者保存用户范围的 Provider 凭据，后者只用于 AniMemo 服务端与外部 Integration Gateway 的通信。

## 当前能力

当前账号 Provider 为 `bangumi`，支持两种经过官方契约确认的授权方式：

- OAuth 2 authorization code：浏览器跳转至 `https://bgm.tv/oauth/authorize`，服务端在 `https://bgm.tv/oauth/access_token` 交换或刷新令牌。
- Personal Access Token：作为明确标注的备用连接方式，由 AniMemo 服务端使用 `Authorization: Bearer <token>` 验证。

两种方式都必须通过 `GET https://api.bgm.tv/v0/me` 验证后才会写入连接。服务端以返回的稳定 numeric user ID 作为权威身份，并保存 username、display name 与必要头像摘要。一个 AniMemo 用户对每个 Provider 最多连接一个账号；同一个 Bangumi numeric user ID 也不能被另一个 AniMemo 用户重复连接。

本实现于 2026-08-09 核对以下官方来源：

- [Bangumi API 文档](https://bangumi.github.io/api/)
- [bangumi/api](https://github.com/bangumi/api)（检查提交 `65d29cff2331e08c0110d30d515f7f1b6488f845`）
- [bangumi/server OpenAPI](https://github.com/bangumi/server/blob/master/openapi/v0.yaml)（检查提交 `10084d67069e6de6275b085775987cf8f9c708e1`）
- [官方 OAuth 说明](https://github.com/bangumi/api/blob/master/docs-raw/How-to-Auth.md)

OAuth code 的官方 TTL 为 60 秒；access token 响应记录 `expires_in`，文档示例为 604800 秒。AniMemo 自身的 CSRF state 默认 10 分钟有效。

## API

- `GET /api/external-accounts/`：列出能力与当前连接摘要。
- `POST /api/external-accounts/{provider}/connect/`：验证并连接 PAT。
- `POST /api/external-accounts/{provider}/authorize/`：创建 OAuth state 并返回官方授权 URL。
- `GET /api/external-accounts/{provider}/callback/`：消费 state、交换 code、验证 `/v0/me` 后固定跳回前端。
- `POST /api/external-accounts/{provider}/verify/`：重新验证；OAuth 临近到期时先刷新凭据。
- `DELETE /api/external-accounts/{provider}/`：删除本地连接与密文。

Bangumi 当前官方契约中未发现 token revoke endpoint，因此断开时不会伪造撤销请求。用户可以在 Bangumi 侧管理或撤销授权。断开不会删除已经导入的手账条目、外部媒体身份、评分、评论或观看记录。

## 凭据安全

凭据 JSON 使用共享 `config.credentials.CredentialCipher` 和 `CREDENTIAL_ENCRYPTION_KEY` 进行 Fernet authenticated encryption，数据库只保存带版本的密文。加密 key 缺失、密文损坏或负载格式错误时均 fail closed，并把连接标记为需要重新授权。

- API serializer 和 Django admin 不返回或展示密文。
- callback URL 不携带 code 或 token 回前端，只返回固定结果和稳定错误代码。
- access token 不写入日志，也不进入 `localStorage` 或 `sessionStorage`。
- OAuth state 由加密安全随机数生成，数据库只存 SHA-256 digest；state 绑定用户、短 TTL、行锁单次消费。
- Provider 只访问代码内固定 HTTPS endpoint，不接受用户 URL。
- 连接、预览和应用接口均有用户权限与限流。

相关环境变量见 `.env.example`；OAuth client secret 只允许通过环境提供，示例文件保持空值。未配置 OAuth 时，网站与 Phase A/B 的 Bangumi 搜索、绑定、刷新仍可正常工作。
