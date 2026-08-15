# AniMemo Deployment Boundary v1

**Status:** FROZEN FOR v1.1
**Version:** v1
**Scope:** 冻结 AniMemo 服务与宿主机公网基础设施之间的职责边界，包括应用栈、配置、生命周期、监听端点、反向代理接口、升级与故障归属。
**Non-goals:** 本文不实现 Installer、Managed Proxy、Backup、Restore、Migration、Doctor，不规定特定面板、DNS、TLS、反向代理或服务器供应商。
**Compatibility:** 保持 API v1、Auth、Resource Identity、Plugin SDK v2、Integration Protocol v1、Release Identity、Deployment Contract 与 Updater fail-closed 行为不变。v1.0 的 1Panel/OpenResty/certbot 路径仅作为当前生产与 legacy evidence，不成为 v1.1 Installer 依赖。
**Change policy:** v1.1 内兼容性澄清可以追加；任何改变 ownership、安全默认、Release Authority、监听暴露范围或升级职责的修改都属于 Contract 变更，必须有显式架构记录、兼容性分析与评审，不得静默修改。

## Boundary principle

AniMemo 负责：**可靠地在服务器上运行 AniMemo 服务。**

管理员负责：**决定并实现如何把 AniMemo 暴露到公网。**

应用安装成功以 AniMemo-owned 服务在已配置的本地监听端点健康为准。DNS、TLS、公网反向代理、防火墙、托管面板以及公网 80/443 端口均不是安装成功条件。

## Capability ownership

| Capability | AniMemo | Administrator | Notes |
| --- | --- | --- | --- |
| Application installation | Owns | Supplies host prerequisites | Installer 只管理 AniMemo-owned 路径与服务。 |
| AniMemo Compose project | Owns | Does not mutate its internals manually | 项目名、服务集合与生命周期由 AniMemo Contract 管理。 |
| PostgreSQL container and data lifecycle | Owns | Supplies disk and host runtime | 指标准 AniMemo Compose 内的 PostgreSQL；外部数据库由管理员运维。 |
| Redis container and data lifecycle | Owns | Supplies disk and host runtime | 指标准 AniMemo Compose 内的 Redis；外部 Redis 由管理员运维。 |
| Migration and bootstrap jobs | Owns | Authorizes intended operation | 必须是显式、scoped job，不得隐藏在 API 启动中。 |
| API and Web services | Owns | Does not replace their lifecycle tooling | 包括 scoped start、stop、reload、health check。 |
| Updater | Owns | Installs/authorizes host service and credentials | Updater 保持固定操作集合与 fail-closed 行为。 |
| Application configuration | Owns schema, validation and atomic update | Supplies instance-specific values | 配置失败不得扩散为公网基础设施修改。 |
| Release identity verification | Owns | May provide least-privilege availability credentials | Release Authority 仍是 GitHub Release 与 GHCR exact OCI digest。 |
| Local listen endpoint | Owns binding and health | Chooses an allowed address/port | 默认 loopback；非 loopback 必须显式选择并警告。 |
| Public origin value | Validates and applies application identity | Chooses canonical external origin | 不表示 AniMemo 拥有该域名、DNS、TLS 或代理。 |
| DNS | Does not manage | Owns | 不是安装成功条件。 |
| TLS certificate | Does not manage | Owns | 不是安装成功条件。 |
| Public reverse proxy | Does not manage | Owns | 可使用任意满足接口的实现。 |
| Public firewall and ports 80/443 | Does not manage | Owns | AniMemo 不开放、占用或修改这些端口。 |
| Hosting panel | Does not require or manage | Optional administrator choice | 1Panel、宝塔、aaPanel 等不属于产品依赖。 |
| Host OS and Docker daemon | Uses through bounded interfaces | Owns | AniMemo 不执行全局 daemon、package 或 prune 操作。 |
| Public reachability monitoring | Provides application health signals | Owns edge-to-origin reachability | 两侧证据用于定位 shared-boundary 故障。 |

## AniMemo-owned

AniMemo-owned 范围包括：

- application installation 与 AniMemo Compose project；
- 标准 Compose 内的 PostgreSQL、Redis、API 与 Web；
- Updater、migration jobs、bootstrap 与 health checks；
- AniMemo-owned application/data/updater/runtime 目录；
- AniMemo-scoped service lifecycle；
- application configuration 的 schema、验证、原子写入、scoped reload、health check 与失败回滚；
- immutable release identity、Release Manifest、checksums、deployment contract、provenance、attestation 与 exact OCI digest 的验证。

“AniMemo owns PostgreSQL/Redis”表示标准 Compose 安装负责其容器、数据挂载和实例级生命周期，不授权 Updater 在普通应用更新中重启数据库，也不授权操作共享或外部 PostgreSQL/Redis。普通 Updater operation 继续保持窄 allowlist：验证目标、必要时备份、运行显式 migration/bootstrap、切换 API/Web、观察健康状态；不得因此扩大到宿主机或共享服务。

## Administrator-owned

管理员负责：

- 受支持的宿主机、CPU 架构、磁盘、文件系统与 Docker/Compose 前置条件；
- DNS 记录、域名注册与域名供应商；
- TLS 证书、续期与 TLS termination；
- 公网 Nginx/OpenResty、Caddy、Traefik、Nginx Proxy Manager 或其他反向代理；
- Cloudflare、Cloudflare Tunnel 或其他网络/CDN/隧道服务；
- firewall、security group、公网入口以及 80/443 端口；
- 1Panel、宝塔、aaPanel 或其他 hosting panel；
- 外部数据库、外部 Redis 及其他共享宿主机服务；
- Host 上最小权限凭据的安装、轮换与撤销；
- 将管理员选择的 Public Origin、listen override 和外部服务参数提供给 AniMemo。

管理员不得通过手工修改 Compose 内部状态、Updater journal 或受管目录来替代 AniMemo lifecycle；需要恢复或协调时应使用已定义的产品接口。

## Shared boundary

共享边界只有明确的输入与观察接口：

1. 管理员选择 listen 与 Public Origin；AniMemo 验证并保存应用配置。
2. AniMemo 在本地监听端点提供 HTTP 服务与 health signal；管理员的代理把公网流量转发到该端点。
3. 管理员负责代理传递正确的 Host、scheme 与受信代理信息；AniMemo 负责按配置校验这些信息。
4. AniMemo 报告 local application health；管理员验证 DNS、TLS、proxy 与 firewall 的端到端状态。
5. 管理员授权 install/update 等意图；AniMemo 仅在 scoped boundary 内执行并验证结果。

共享边界不产生共同 ownership：Public Origin 是应用身份输入，不把 DNS/TLS/proxy ownership 转移给 AniMemo；local health 成功也不证明公网链路健康。

## Explicitly unsupported automation

AniMemo v1.1 不提供 Managed Proxy Mode，也不要求或自动执行：

- 1Panel、宝塔、aaPanel、Nginx Proxy Manager 或其他 panel 配置；
- DNS provider 或 Cloudflare API 修改；
- Cloudflare Tunnel 创建或更新；
- Let's Encrypt/certbot 申请、续期或安装证书；
- public Nginx/OpenResty、Caddy、Traefik 配置、测试或 reload；
- firewall、security group、公网 80/443 端口修改；
- Docker daemon restart、全局 prune、全局 package upgrade 或其他 Compose project 操作。

不得增加 `--setup-nginx`、`--setup-caddy`、`--setup-cloudflare`、`--setup-certbot` 等 Installer 模式。未来若有真实生产证据需要重新讨论，必须形成新 Contract 版本；不得在 v1 中隐式加入。

## Security model

- 默认 listen 必须是 loopback，避免安装完成即产生未加密公网暴露。
- AniMemo 不需要 root-owned public edge；宿主机高权限仅授予安装和受限 Host Updater 所需的最小范围。
- Django/API 容器不得获得 Docker socket；需要 Docker 权限的操作留在受限 Host Updater。
- Release Consumer 必须独立验证正式 Release Authority；bootstrap transport、面板或安装域名均不能成为第二发布权威。
- 所有 Host mutation 必须限制在 AniMemo-owned 路径、Compose project 与服务；禁止全局或共享服务操作。
- Secret 与 Host credentials 不进入 Release、日志、Operation journal、应用 API 或公共安装输出。
- 显式 direct-access mode 不改变管理员对 TLS、firewall 与网络暴露的责任。

## Failure ownership

| Failure | Primary owner | Required behavior |
| --- | --- | --- |
| Release verification or compatibility failure | AniMemo | Fail closed；不得 pull/migrate/switch。 |
| Compose service、migration、bootstrap 或 local health failure | AniMemo | 报告 scoped diagnosis；按 Contract rollback 或进入人工恢复。 |
| AniMemo-owned path permissions/state failure | AniMemo | 安全停止；不得删除未知内容或越界修复。 |
| Port collision on configured local endpoint | Shared | AniMemo 明确报告；管理员选择可用端口；不得杀进程或随机改端口。 |
| Invalid Public Origin/application config | Shared | AniMemo 拒绝或回滚；管理员修正期望值。 |
| DNS、certificate、TLS、public proxy、firewall failure | Administrator | 管理员修复；AniMemo 不自动修改外部基础设施。 |
| Proxy reaches wrong host/scheme or cannot reach loopback | Shared | 管理员检查代理；AniMemo提供 local health/config evidence。 |
| Shared Docker daemon/host resource failure | Administrator | 管理员恢复宿主机；AniMemo不得全局重启或 prune。 |

## Upgrade ownership

AniMemo Updater 负责验证 exact Release、裁决 compatibility、执行必要的 scoped backup gate、显式 migration/bootstrap、切换 API/Web、health observation 与 CURRENT/PREVIOUS 状态维护。

管理员负责发起或批准升级、保证宿主机与网络能够访问正式 Release Authority、维护所需的最小权限凭据，并处理 Updater 明确报告的宿主机或公网基础设施问题。

升级不得修改 DNS、TLS、public proxy、firewall、hosting panel、Docker daemon、共享 PostgreSQL/Redis 或其他 Compose project。Public Origin 或 listen 的变更属于应用配置操作，不得借升级隐式完成。

## Reverse proxy expectations

AniMemo 只要求管理员选择的反向代理能够：

- 转发到已配置的 AniMemo listen endpoint；
- 保留或正确设置应用所需的 Host 与 scheme 信息；
- 对 Web 与 API 使用一致的 canonical Public Origin；
- 允许应用 health 与必要路由正常通过；
- 自行承担 TLS termination、证书续期、访问日志、公网限流与 edge security。

AniMemo 不偏好或探测特定代理品牌，不写入代理配置，不 reload 代理，也不以检测到代理为安装成功条件。代理示例只能是非权威参考，不能成为 Installer workflow。

## Public origin semantics

Public Origin 是用户通过浏览器访问 AniMemo 的 canonical external origin。它参与应用身份、安全来源校验、absolute URL 与外部 provider callback 派生，但不表示 AniMemo 控制对应的 DNS、TLS 或 reverse proxy。

Public Origin 与 listen 是两个独立值：Public Origin 描述外部身份；listen 描述本机服务入口。改变 Public Origin 只允许执行应用侧验证、原子配置更新、AniMemo-scoped reload、health check 与失败回滚，并提示管理员另行检查 DNS、TLS、proxy 及外部 provider callback。

Public Origin 的详细语义由 [`Public Origin / Listen Contract v1`](public-origin-listen-contract-v1.md) 冻结。

## Loopback default

默认 listen 为 `127.0.0.1:8088`。`8088` 是可替换的默认端口，不是协议；安全不变量是 **loopback by default**。

端口冲突时 AniMemo 必须明确失败并报告冲突，可以建议另一个 loopback 端口，但不得杀死占用进程、自动选择随机端口、修改 firewall 或回退到 `0.0.0.0`。

非 loopback listen 只允许显式 opt-in，并必须显示有关无 HTTPS、Secure Cookie、OAuth/provider callback、Turnstile、网络暴露和 firewall 责任的警告。它不是安装默认值，也不把公网安全责任转移给 AniMemo。

## Legacy production evidence

仓库中的 1Panel app root、OpenResty 站点配置、certbot cron、当前生产域名与 `deploy/deploy.sh` 行为记录 v1.0 current-production/legacy history。它们可以为兼容迁移和事故恢复提供 evidence，但：

- 不定义 v1.1 的产品部署边界；
- 不成为 v1.1 Installer 的依赖或默认值；
- 不授权再次执行历史 `/data/anime-journal` 到 `/data/animemo` 迁移；
- 不授权 Installer 写入/reload OpenResty 或执行证书自动化；
- 不得为采用标准路径而未经协调直接移动当前生产实例。

标准路径迁移必须由 [`Filesystem Layout v1`](filesystem-layout-v1.md) 定义兼容与识别语义，并保持现有 Updater/rollback 在完成协调切换前可用。

## Future compatibility

本文只冻结未来组件可以依赖的 interface expectation，不实现这些组件：

- **Installer:** 发现并写入 AniMemo-owned roots 与实例 metadata；建立 loopback 服务；不得配置公网基础设施；信任链见 [`Installer Contract v1`](installer-contract-v1.md)。
- **Backup:** 根据 filesystem classification 备份实例持久状态；数据库使用 logical dump，不把 live PostgreSQL data directory 打包为备份。
- **Restore:** 根据 instance metadata 与标准 roots 验证目标，只恢复 AniMemo-owned persistent state，不修改公网基础设施。
- **Migration:** 理解 app/data/updater roots 与 application identity，移动实例而不接管 DNS/TLS/proxy；不得重跑 legacy data-path migration。
- **Doctor:** 发现 listen、Public Origin、roots、Compose 与 Updater 状态；把 application failure 与 administrator-owned edge failure 分开报告。
- **Updater:** 发现 canonical installation root，继续使用固定、窄 allowlist 和 exact Release verification；不得因路径标准化扩大 Host 权限。

路径、ownership、persistence、backup/migration/restore 分类以 [`Filesystem Layout v1`](filesystem-layout-v1.md) 为准。Release Authority 与升级安全语义继续以 [`Release Contract v1`](release-contract-v1.md) 和 [`Update Agent v1 Contract`](update-agent-v1.md) 为准。

## Compatibility statement

本 Contract 不改变 API v1、认证、资源主键、Plugin SDK v2、Integration Protocol v1、Release Manifest、Deployment Contract、首次安装 identity 或 Updater fail-closed 语义。它只把此前散落在生产文档、Compose 与 legacy 脚本中的 deployment ownership 提升为 provider-neutral 的 v1.1 canonical boundary。
