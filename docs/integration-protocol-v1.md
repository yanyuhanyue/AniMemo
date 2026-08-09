# Integration Protocol v1

AniMemo 的 Integration Gateway 是 provider-neutral 的外部连接协议。外部服务器凭连接级 HMAC 访问网关；这组凭证与 AniMemo 用户的密码、JWT、Cookie 和 Cloudflare Access 完全不同。

## Connection

管理员通过 Django 管理命令创建连接，密钥只在命令输出中显示一次：

```text
python manage.py integration_connection create --provider <provider> --instance-id <instance> --name <name>
python manage.py integration_connection rotate-secret <connection-id>
```

数据库只保存 `CREDENTIAL_ENCRYPTION_KEY` 保护的 Fernet 密文。`provider` 是配置数据，协议核心不包含任何具体平台行为。

## HMAC

请求使用以下请求头：

```text
X-AniMemo-Key-Id: <key-id>
X-AniMemo-Timestamp: <unix-seconds>
X-AniMemo-Nonce: <unique-request-nonce>
X-AniMemo-Signature: v1=<lowercase-hex-sha256-hmac>
```

规范化输入为六行：

```text
ANIMEMO-HMAC-V1
<TIMESTAMP>
<NONCE>
<METHOD>
<PATH_WITH_QUERY>
<SHA256_BODY>
```

伪代码示例（`SHARED_SECRET` 是外部安全配置，不是 AniMemo 用户凭证）：

```python
import hashlib
import hmac

canonical = "\n".join([
    "ANIMEMO-HMAC-V1",
    timestamp,
    nonce,
    method.upper(),
    path_with_query,
    hashlib.sha256(body).hexdigest(),
]).encode("utf-8")
signature = "v1=" + hmac.new(SHARED_SECRET.encode(), canonical, hashlib.sha256).hexdigest()
```

时间戳允许约 `±300` 秒；nonce 使用共享缓存原子占用，默认保留 `660` 秒。签名比较使用 `hmac.compare_digest`。生产请求应使用 HTTPS；Django 继续使用现有 `SECURE_PROXY_SSL_HEADER` 和可信代理配置识别转发的 HTTPS。

## Pairing 与 Identity Binding

已登录用户请求 `/api/integrations/v1/pairing-codes/`，得到一次性、连接作用域、用户作用域的短码。默认 TTL 为 10 分钟；数据库保存 PBKDF2 密码哈希和不可逆查找指纹，不保存明文配对码。

外部服务器通过 HMAC 调用 `/api/integrations/v1/pair/consume/`：

```json
{
  "code": "ABCD-EFGH",
  "platform": "qq",
  "external_user_id": "123456",
  "display_name": "示例用户"
}
```

连接由 `X-AniMemo-Key-Id` 认证结果决定，不能由请求体选择。身份唯一性是 `connection + platform + external_user_id`。绑定默认只允许私聊投递，`allow_group_delivery` 默认 `false`，v1 不实现自动群投递。

## Actions

Manifest v2 可选声明：

```json
{
  "extensions": ["backend.api", "integration.actions", "integration.events"],
  "integrations": {
    "actions": [{"name": "import-text", "description": "导入文本"}],
    "events": [{"name": "import-completed", "description": "导入完成"}]
  }
}
```

插件 Runtime 注册本地名：

```python
host.integrations.register_action("import-text", handler)
```

宿主自动公开为 `<plugin-slug>.import-text`。动作处理上下文只提供已解析的 AniMemo `user`、连接元数据、平台、外部用户 ID 和 `request_id`，不提供密钥、密文、签名或重新选择绑定的能力。

动作处理顺序固定为：

```text
HMAC
→ IntegrationConnection
→ ExternalIdentityBinding
→ AniMemo user
→ USER 插件安装且 enabled
→ 当前健康已发布 Runtime
→ Manifest 声明且 Runtime 已注册的 action
→ IntegrationActionReceipt
```

`payload.user_id` 等字段只是普通不可信 payload，不能改变宿主解析出的用户。`installationMode=user` 时，未安装或已禁用都拒绝，网关不会自动安装插件。

每个 `connection + request_id` 只有一个收据。已完成重试直接返回已存响应；并发重复请求在收据完成前不会再次执行副作用动作。

## Events、Poll 与 ACK

插件只能调用：

```python
host.integrations.emit(user, "import-completed", {"count": 3})
```

宿主从该 AniMemo 用户的已启用绑定解析目标，每个绑定创建一条私聊事件。插件不能传入 connection、external user 或群 ID。

外部服务器使用连接自己的 HMAC 拉取：

```text
GET /api/integrations/v1/events/?after=<cursor>&limit=50&wait=1
POST /api/integrations/v1/events/ack/
{"event_ids": [1, 2, 3]}
```

`limit` 最大 100，`wait` 最大 25 秒。事件使用数据库单调递增主键作为稳定 `event_id`，连接只能读取和 ACK 自己的事件。可通过系统 cron 定期运行 `python manage.py cleanup_integration_events`：已 ACK 事件默认保留 1 天，未 ACK 事件默认保留 7 天。

## Credential Boundary

外部服务器连接凭证 != AniMemo 用户凭证。网关通过绑定解析 AniMemo 用户，插件收到解析后的用户对象；插件不能选择外部身份，也不能从外部请求中选择另一个 AniMemo 用户。

## AstrBot Bridge 示例

`astrbot_plugin_animemo_bridge` 是一个 provider-neutral 的协议客户端示例：它只把 AstrBot 提供的 `platform + external_user_id` 作为外部身份，把私聊 UMO 保存在 AstrBot 官方 plugin data directory，并用 HMAC 调用本协议。协议本身不依赖 AstrBot API；AstrBot 的命令、MessageChain 和生命周期适配只存在于 Bridge 包中。

Reference integration `watch-history-importer` 0.3.2 声明以下动作（协议本地名使用规范 kebab-case）：`history-get`、`history-add`、`entries-search`、`import-preview`、`import-commit`；事件为 `history-updated`、`import-completed`。外部调用名形如 `watch-history-importer.history-get`。
