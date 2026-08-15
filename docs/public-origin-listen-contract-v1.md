# Public Origin / Listen Contract v1

- **Status:** FROZEN FOR v1.1
- **Version:** v1
- **Scope:** AniMemo 的宿主机监听端点、canonical external origin、派生的应用安全配置、Bangumi OAuth callback，以及未来 `animemo config` 的事务边界。
- **Non-goals:** 不配置 DNS、TLS、证书、公网反向代理、Cloudflare、firewall、80/443；不在本阶段实现 CLI。
- **Compatibility:** 保持 API v1、Auth、Resource Identity、Plugin SDK v2、Integration Protocol v1、Release Identity 与 Updater fail-closed 行为不变。`https://animemo.cc` 只是当前实例配置，不是产品默认域名。
- **Change policy:** 改变默认绑定范围、origin 规范化、callback 路径或配置事务语义属于 Contract 变更，必须记录兼容与迁移方案并通过相关 gates；不得静默修改。

本文与 [Deployment Boundary v1](deployment-boundary-v1.md)、[Filesystem Layout v1](filesystem-layout-v1.md) 和 [Installer Contract v1](installer-contract-v1.md) 共同定义 v1.1 Durable Deployment 的底层接口。

## Three separate concepts

| Concept | Meaning | Authority | Must not imply |
| --- | --- | --- | --- |
| Listen endpoint | AniMemo Web 在宿主机接受连接的地址与端口 | AniMemo instance config | DNS、TLS 或公网可达 |
| Public Origin | 用户在浏览器实际访问 AniMemo 的 canonical external origin | AniMemo application identity | AniMemo 拥有该域名、证书或代理 |
| Public reverse proxy | 将公网请求转发到 Listen endpoint 的管理员设施 | Administrator | Release、instance 或 application identity |

这三者不得合并为一个“域名/端口”字段。Public Origin 可以在 DNS、TLS 和反向代理就绪前配置；Listen endpoint 健康也不证明 Public Origin 已可从公网访问。

## Listen contract

### Default

正式默认值是：

```text
127.0.0.1:8088
```

- 安全 invariant 是 **loopback by default**。
- `8088` 只是可替换的默认端口，不是协议、release identity 或兼容性版本。
- 标准 Compose 必须在宿主机侧显式绑定 loopback；容器内部监听地址不改变宿主机暴露边界。
- Installer、Updater 与 Doctor 的本地探测必须直接使用配置后的 loopback endpoint，不应依赖公网 DNS 回环。

当前 `deploy/docker-compose.yml` 已使用 `127.0.0.1:${ANIMEMO_PORT:-8088}:80`；v1.1 不得把该默认放宽为 `0.0.0.0`。

### Alternate loopback port

未来接口允许显式配置其他可用端口，例如：

```bash
sudo animemo config listen 127.0.0.1:18088
```

或首次安装时：

```bash
sudo sh /tmp/animemo-install.sh --listen 127.0.0.1:18088
```

端口必须是 `1..65535`。实现可以支持 `127.0.0.0/8` 或 bracketed IPv6 loopback `[::1]:PORT`，但不得因解析失败退回 wildcard bind。

### Port collision

如果请求的 endpoint 已被占用，Installer 或配置命令必须：

1. 在任何 service mutation 前检测冲突；
2. 报告请求的 endpoint 已不可用；
3. 保持现有进程、firewall 与 instance state 不变；
4. 允许操作者显式选择另一 loopback 端口。

默认不得杀进程、抢占端口、随机选择端口、自动改 firewall，或改成 `0.0.0.0`。工具可以给出确定性的可用端口建议，但建议不等于自动接受。

## Direct access mode

非 loopback 监听只允许作为显式 opt-in，例如用户明确输入 `--listen 0.0.0.0:8088`。它绝不成为安装 fallback，也不因检测不到反向代理而自动启用。

启用前必须显示醒目警告，至少覆盖：

- 当前连接可能没有 HTTPS；
- Secure Cookie 在 HTTP 下不可用；
- OAuth 与 Bangumi callback 仍由 Public Origin 决定；
- Turnstile 等依赖真实 browser origin 的能力可能不工作；
- 监听会扩大网络暴露；
- firewall 与公网访问控制完全由管理员负责。

显式非 loopback `--listen` 本身构成 direct-access opt-in；交互模式还应要求确认。`--non-interactive` 只能在调用者明确提供该非 loopback 值时继续，并仍须输出警告。仅设置 `0.0.0.0` 不得自动合成 Public Origin。

## Public Origin semantics

Public Origin 是一个规范化的 HTTP(S) origin，例如：

```text
https://animemo.example.com
```

它必须包含 scheme 与 host，可以包含显式端口，但不得包含：

- 用户名或密码；
- path；
- query；
- fragment；
- wildcard。

末尾 `/` 必须被规范化移除。正常生产身份必须使用 HTTPS；任何临时 HTTP direct-access profile 都必须显式启用并接受上一节的全部限制。

首次安装必须取得一个有效的预期 Public Origin，并将其写入受保护的 instance config；但安装成功不要求该 origin 当时已完成 DNS 解析、TLS 签发、反向代理配置或公网探测。安装验收使用本机 Listen endpoint，加上由 Public Origin 派生的 `Host` 与 forwarded scheme 做 AniMemo-scoped health check。

## Derived application identity

Public Origin 是以下配置的 source of truth：

| Effective setting | Required relation |
| --- | --- |
| `ANIMEMO_PUBLIC_ORIGIN` | 等于规范化的 Public Origin |
| `ALLOWED_HOSTS` | 必须包含 Public Origin 的 host，不含 scheme/path/wildcard |
| `CORS_ALLOWED_ORIGINS` | 必须包含完整、精确的 Public Origin |
| `CSRF_TRUSTED_ORIGINS` | 必须包含完整、精确的 Public Origin |
| `FRONTEND_URL` | 由 Public Origin 派生，不建立第二 authority |
| OAuth/provider callback | 由 Public Origin 加冻结路径派生 |
| 其他 public absolute URL | 使用同一 Public Origin，不 hardcode 实例域名 |

实现可以保留管理员明确配置的额外 trusted origins，但不得用 wildcard，也不得让额外值替代 canonical Public Origin。改变 Public Origin 时，Installer 管理的旧 canonical host/origin 必须被新值替换，避免无意永久信任旧域名。

## Bangumi callback

Bangumi OAuth callback 固定为：

```text
ANIMEMO_PUBLIC_ORIGIN
+ /api/v1/external-accounts/bangumi/callback/
```

例如当前实例得到：

```text
https://animemo.cc/api/v1/external-accounts/bangumi/callback/
```

`https://animemo.cc` 不是 product constant。callback 不得单独配置、不得从请求头动态猜测，也不得被数据库中的 provider credentials 覆盖。现有 `backend/config/settings.py` 与 `backend/config/test_public_origins.py` 已覆盖该派生关系。

## Domain configuration transaction

未来接口：

```bash
sudo animemo config domain https://new.example.com
```

它只修改 AniMemo application identity，固定执行：

```text
Validate
→ Atomic Config Update
→ AniMemo-scoped Reload
→ Local Health Check
→ Commit
```

任一步失败都必须回滚 config 与 AniMemo-scoped runtime 到先前可用状态，并再次做本地 health check；如果回滚也失败，必须保留可诊断状态并明确报告，不能伪报成功。

成功后必须提示管理员自行核对：

- DNS；
- TLS certificate；
- Public reverse proxy；
- OAuth provider callback allowlist；
- Bangumi 或其他外部 provider callback 设置。

该命令不得调用 Cloudflare/DNS API、申请证书、改 Nginx/OpenResty/Caddy/Traefik、改 firewall，或占用 80/443。

## Listen configuration transaction

未来 `animemo config listen HOST:PORT` 同样使用 validate、atomic update、AniMemo-scoped reload、health check、rollback 流程。配置命令必须先验证新 endpoint 可绑定，保留旧 endpoint 直到切换准备完成，并且只重载 AniMemo-owned runtime。它不得修改 public proxy；切换成功后应提醒管理员自行更新 proxy upstream。

## Reverse proxy expectations

管理员选择的反向代理必须：

- 将 Public Origin 的请求转发到配置后的 Listen endpoint；
- 保留或设置正确的 `Host`；
- 设置真实的 forwarded scheme；
- 只从管理员配置的可信代理路径传递 client IP；
- 支持 AniMemo 当前需要的普通 HTTP 转发语义；
- 自行终止 TLS 并维护证书。

AniMemo 可以验证可信代理配置与请求语义，但不得选择、安装或重载代理实现。

## Configuration precedence audit

| Layer | CURRENT | TARGET | GAP | MIGRATION NEED |
| --- | --- | --- | --- | --- |
| Compose listen | 固定 host `127.0.0.1`，端口读取 `ANIMEMO_PORT`，默认 `8088` | Versioned instance config 表示完整 listen endpoint | 目前只可改端口，direct access 尚无 canonical runtime interface | 后续 Installer/CLI 实现；本轮不改 Compose |
| Runtime environment | Compose/Updater 从 app root 的 `.env.production` 读取 | 受保护、持久且与 replaceable app material 分离的 instance config | 当前配置与 application tree 同 lifecycle | 按 Filesystem Contract 做未来显式迁移 |
| Django Public Origin | `ANIMEMO_PUBLIC_ORIGIN`；生产缺省仍指向当前实例 `https://animemo.cc` | v1.1 Installer 必须显式写入，缺失时 fail closed | 当前实例 fallback 容易被误认为产品默认 | 兼容现有 v1.0；v1.1 新安装不得依赖 fallback |
| Allowed/CORS/CSRF | 生产要求显式非空并包含 Public Origin | 由同一 managed config 原子维护 | 尚无 `animemo config domain` | 后续 CLI 实现 |
| Bangumi callback | 已由 Public Origin 派生 | 保持 | 无 callback identity gap | 无数据迁移 |
| Updater health | 读取 Public Origin host/scheme 与 `ANIMEMO_PORT`，连接 loopback | 从 versioned instance metadata/config 发现 listen 与 origin | custom roots/listen 尚无 discovery record | 后续 additive metadata |

环境变量兼容可以保留，但优先级必须只有一个持久 source of truth。镜像中的开发/历史默认值不是生产 instance identity，也不是 Installer authority。

## Instance discovery interface

Installer 必须原子写入 versioned、非 secret 的 `/var/lib/animemo-updater/instance.json`。Updater 与未来 Doctor 通过它发现至少以下逻辑字段：

- installation profile/schema version；
- app root 与 data root；
- Listen endpoint；
- Public Origin；
- 已验证的 release identity；
- managed config 的位置。

字段的最终序列化 schema 在 Installer 实现前通过 contract test 固定；本阶段不实现 reader/writer。metadata、managed config、systemd allowlist 或实际 Compose 配置彼此不一致时必须 fail closed。metadata 不得包含 secret。

## Security model

- Loopback default 缩小未配置 proxy/firewall 时的暴露面，但不是认证或 TLS 的替代品。
- Public Origin 只能来自受保护 config，不能信任任意请求的 `Host`/forwarded headers 来重写 application identity。
- CORS、CSRF 与 Allowed Hosts 必须精确，不允许 wildcard convenience mode。
- 配置文件与 instance metadata 的权限、backup/migration 分类由 [Filesystem Layout v1](filesystem-layout-v1.md) 冻结。
- 日志、dry-run 和错误消息不得输出 secret 或完整 credential。

## Failure ownership

| Failure | AniMemo responsibility | Administrator responsibility |
| --- | --- | --- |
| Loopback bind/health failure | 检测、回滚 AniMemo config/runtime、报告 | 选择未占用端口或处理外部进程 |
| Invalid Public Origin | 拒绝并保持旧配置 | 提供正确 canonical origin |
| DNS 不解析 | 提示外部检查，不改 DNS | 修复 DNS |
| TLS 无效/过期 | 提示外部检查，不签发证书 | 修复 certificate/TLS terminator |
| Proxy 502/headers 错误 | 提供健康 endpoint 与期望 | 修复 proxy upstream/headers |
| OAuth callback 未登记 | 展示派生 callback | 更新 provider 配置 |
| Firewall 阻断 | 不改 firewall | 修复 host/provider firewall |

## Future compatibility

- **Installer** 写入并验证 listen、Public Origin、managed config 与 instance metadata。
- **Updater** 从受验证 metadata/config 发现 roots 与本地 health endpoint，不接受 Django RPC 提供任意 host path。
- **Backup** 备份 canonical application config 与必要 identity metadata，但不把 DNS/TLS/proxy 当作 AniMemo payload。
- **Restore** 恢复 application identity 后仍要求管理员重新核对外部基础设施。
- **Migration** 可以改变 Public Origin 与 Listen endpoint，但必须保持两者语义独立。
- **Doctor** 分别报告 application health、loopback listen、Public Origin config、DNS、TLS 与 proxy 状态；不得把其中一个 PASS 代替另一个。

Backup、Restore、Migration、Migration Secret Envelope、Doctor 与 Compatibility Matrix 的实现全部 deferred to Phase 2+。
