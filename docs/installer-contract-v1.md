# AniMemo Installer Contract v1

Status: FROZEN FOR v1.1

Version: v1

Scope: 冻结 AniMemo 新实例安装器的发布权威、bootstrap 信任边界、版本选择、参数语义、前置检查、幂等行为、实例发现接口和失败行为。

Non-goals: 本 Contract 不实现 install.sh、animemo CLI、Updater 路径切换、Backup、Restore、Migration、Doctor、DNS、TLS、反向代理或生产部署。

Compatibility: 保持 Release Contract v1、Exact Artifact Semantics、First-run Installation Identity 和 Update Agent v1 的 fail-closed 行为不变；v1.1 是 pre-production clean break，只支持 canonical roots 与 `v1.1-standard` profile，不读取、识别、adopt 或迁移 pre-v1.1 layout。

Change policy: 本文中的 MUST、MUST NOT、SHOULD 和 MAY 是规范性要求。改变 Release Authority、默认路径、默认监听、channel 解析、实例定位或幂等矩阵属于 Contract 变更，必须先记录兼容性影响并经独立审查；不得通过实现细节静默改变语义。

## 1. 相关 Contract

Installer v1 必须与以下 Contract 一致：

- [Deployment Boundary v1](deployment-boundary-v1.md)
- [Filesystem Layout v1](filesystem-layout-v1.md)
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)
- [Release Contract v1](release-contract-v1.md)
- [Update Agent v1](update-agent-v1.md)

本文中的 deployment contract artifact 专指 GitHub Release 内的 deployment-contract.json 及其所绑定的 Compose 文件身份，不等同于 Deployment Boundary v1，也不替代 Filesystem Layout v1。

## 2. 安装成功的定义

安装成功只表示：

1. 目标 release identity 已从唯一 Release Authority 完整验证；
2. AniMemo-owned 文件和目录已按 Filesystem Layout v1 创建；
3. PostgreSQL、Redis、migration、bootstrap、API、Web 和 Updater 的 AniMemo-scoped lifecycle 已建立；
4. 服务在配置的本地 listen endpoint 上通过健康检查；
5. instance locator 已原子写入并与实际路径和 systemd allowlist 一致；
6. 首次管理员初始化仍由既有一次性 setup-code 流程完成。

安装成功不要求 DNS 已传播、TLS 证书已签发、公共反向代理已配置或 Public Origin 已能从公网访问。上述事项属于管理员边界。

## 3. Bootstrap endpoint 与信任边界

目标入口为：

    curl -fsSLo /tmp/animemo-install.sh https://install.animemo.cc/install.sh
    sudo sh /tmp/animemo-install.sh

install.animemo.cc 的唯一职责是运输 bootstrap program bytes。

它 MUST NOT：

- 返回或决定“最新 AniMemo 版本”；
- 返回 authoritative channel resolution；
- 充当 Manifest、checksum、digest、provenance 或 attestation authority；
- 通过自身 JSON、HTML、header、redirect target 或 registry tag 改写 release identity；
- 成为第二 Stable Authority、Mirror Authority 或 Release Source。

### 3.1 Bootstrap code transport trust

Bootstrap program bytes 的运输信任与应用 Release Authority 是两个不同问题。

HTTPS 可以保护从 install.animemo.cc 到管理员的传输，但执行下载脚本本身仍是在信任该 bootstrap code。后续对 Release 的验证不能追溯消除一个已被篡改 bootstrap 在执行前获得的代码权限。文档和实现不得声称“只要脚本内部验证 Manifest，bootstrap endpoint 就完全不需要信任”。

需要抵抗 bootstrap endpoint 篡改的管理员，必须在 sudo 执行前，通过独立于 install.animemo.cc 的 GitHub repository identity 验证 bootstrap program bytes。具体分发和验证机制属于 Installer implementation；在该机制存在前，不得宣称 bootstrap bytes 具备端到端 provenance。

无论 bootstrap bytes 如何运输，它都不能决定应用 Release。应用 Release 必须按下一节重新从唯一 Release Authority 解析并验证。

## 4. 唯一 Release Authority

AniMemo v1.1 继续只有一个正式 Release Authority：

    GitHub Release in yanyuhanyue/AniMemo
    +
    GHCR API/Web repository@sha256:digest

Installer MUST 固定：

- Repository：yanyuhanyue/AniMemo；
- GitHub API authority：https://api.github.com；
- API repository：ghcr.io/yanyuhanyue/animemo-api；
- Web repository：ghcr.io/yanyuhanyue/animemo-web；
- Release Manifest schema 和既有 Release Contract v1；
- GitHub Actions OIDC issuer、repository、workflow、main ref、source commit 和 SLSA predicate identity。

Installer MUST NOT 接受用户、bootstrap endpoint、环境变量或远程响应提供的替代 repository、registry、release URL、manifest URL、attestation URL 或 image repository。

凭据只可以提高固定 authority 的 rate limit 或可用性。凭据不能改变 authority 判定，也不得进入 instance locator、Manifest、日志或错误输出。

## 5. Exact release trust chain

Installer 在任何持久安装 mutation 前，必须完成以下链路：

1. 从固定 GitHub repository 枚举符合 channel 规则的非 draft Release candidate；
2. 将 strict SemVer tag 与 GitHub Release 的 prerelease metadata 绑定；
3. 对选定 exact tag 重新读取 exact GitHub Release metadata；
4. 只接受既有三项 Release assets：
   - release-manifest.json
   - deployment-contract.json
   - checksums.txt
5. 验证 checksums.txt 完整且只绑定 Manifest 与 deployment contract；
6. 验证 Manifest schema、version、channel、40 位 release.commit、provenance.sourceCommit、minimum updater version 和 compatibility contracts；
7. 有界 peel lightweight 或 annotated tag，最终 Git commit 必须等于 release.commit；
8. 验证 deployment-contract.json 的 canonical digest 与 Manifest 中的 deployment identity 完全一致；
9. 验证 API 和 Web 的固定 repository、linux/amd64 platform 与 exact sha256 digest；
10. 验证 API/Web OCI attestations，绑定 release workflow、release.commit、exact subject name/digest、repository、OIDC issuer、main ref 和 SLSA provenance；
11. 验证 Manifest 与 deployment contract attestations，绑定 Manifest 指定的 signing workflow、provenance.sourceCommit 和 exact file subject digest；
12. 要求实际使用的 API/Web image identity 与已验证 Manifest 完全一致。

Stable promotion 中 release.commit 可以是原 RC application commit，而 provenance.sourceCommit 是签署 Stable Manifest 的 promotion workflow commit。Installer 不得混淆这两个身份。

Plan、cache、bootstrap endpoint 响应或先前验证结果都不能代替执行边界的 exact verification。任何字段、asset、checksum、tag、metadata、digest、deployment file 或 attestation 不一致都必须 fail closed。

### 5.1 Program 与 deployment bytes

Installer 实际执行或安装的所有 program/deployment bytes，包括 Compose、Updater program、launcher 和 service assets，必须能够绑定到同一个已验证的 exact release identity。

deployment-contract.json 当前只绑定既有 Release Contract v1 定义的 deployment files。它不是任意 program bytes 的通用签名，也不是完整安装 bundle。

若 Installer 无法使用现有 GitHub Release、exact commit、checksums、deployment contract 和 provenance 证明某个必需 byte 的 exact release binding，则必须在写入目标目录或启动服务前失败。不得改为信任 install.animemo.cc、自建镜像、GitHub source page 的显示版本或 mutable branch。

本阶段不修改 Release Manifest schema、不增加 Release asset allowlist，也不实现新的 bundle。未来实现若发现既有 authority 无法承载所需 bytes，必须回到 Release Contract 评审，不得在 Installer 内发明第二 authority。

## 6. Channel 与版本解析

### 6.1 参数互斥

--channel 与 --version 互斥。用户显式同时提供两者时，Installer 必须在网络访问和持久 mutation 前以 usage error 失败。

未提供 --version 时，--channel 默认 stable。

--version 选择一个 exact immutable tag，不再执行 channel resolution。Installer v1 只接受 strict Stable 或 RC tag；Beta 不属于 Installer v1 的公开 channel contract。

### 6.2 Stable

--channel stable 的解析规则：

1. 枚举固定 GitHub repository 的 Releases；
2. 只保留 draft=false、prerelease=false 且 tag 为 strict vMAJOR.MINOR.PATCH 的 candidate；
3. 按 SemVer 排序，选择版本最高者；
4. 对该 exact tag 执行完整 trust chain。

### 6.3 RC

--channel rc 的解析规则：

1. 枚举固定 GitHub repository 的 Releases；
2. 只保留 draft=false、prerelease=true 且 tag 为 strict vMAJOR.MINOR.PATCH-rc.N 的 candidate；
3. 排除 Stable、Beta 和其他 prerelease；
4. 按 SemVer 排序，选择版本最高者；
5. 对该 exact tag 执行完整 trust chain。

### 6.4 Discovery 不是 authority

Release listing 只发现 candidate。排序不按发布时间、创建时间、API 返回顺序、下载数或网页顺序。

以下内容永远不是 authority：

- GitHub /releases/latest endpoint；
- GitHub Release 的 “Latest” 展示标记；
- latest、stable 或 rc mutable image tag；
- registry latest；
- install.animemo.cc 返回的版本；
- HTML 页面版本号；
- 未验证 JSON；
- 本地 cache 中的旧解析结果。

选中的最高 candidate 如果完整验证失败，Installer 必须失败并报告 authority verification error，不得静默降级安装较旧版本。

### 6.5 Exact version

--version 必须是完整 immutable tag，例如：

    --version v1.1.0
    --version v1.1.0-rc.2

部分版本、范围、通配符、latest、分支、commit-only 输入和 OCI tag 都必须拒绝。Exact version 仍必须通过 GitHub Release metadata、tag peel、Manifest、checksum、deployment contract、attestation 和 OCI digest 的完整验证。

## 7. Installer CLI v1

| 参数 | 默认值 | 规范语义 |
| --- | --- | --- |
| --channel stable | stable | 选择最高 eligible Stable；与 --version 互斥 |
| --channel rc | 无 | 选择最高 eligible RC；与 --version 互斥 |
| --version TAG | 无 | 安装 exact Stable/RC tag；不做 channel resolution |
| --dry-run | false | 完成参数、state、platform、network 和 release verification；零持久 mutation |
| --non-interactive | false | 禁止 prompt；任何缺失输入立即失败 |
| --listen ADDRESS:PORT | 127.0.0.1:8088 | 本地监听；8088 只是默认端口 |

未知参数、重复且冲突的参数、空值、相对路径、非法端口或非法 SemVer 必须在 mutation 前失败。

### 7.1 Dry run

--dry-run MUST NOT：

- 创建或修改 app/data/updater/state/runtime 目录；
- 写入 env、instance.json、systemd、tmpfiles、sysusers 或 launcher；
- pull image 到本地 Docker store；
- 创建、启动、停止或重建 container；
- reload、enable、start 或 restart service；
- 运行 migration、bootstrap、backup 或 restore；
- 修改 DNS、TLS、proxy、firewall 或 port ownership。

Dry run MAY 使用受限临时目录下载 verification material，但必须在退出时清理，且不能把结果当作未来真实安装的执行期 authority。若真实安装按当前状态会失败，dry run 必须返回非零并给出相同的稳定 error class。

### 7.2 Non-interactive

--non-interactive 禁止任何 stdin prompt、TTY confirmation 或隐式默认接受。

Non-interactive 模式的 Public Origin 必须来自显式 ANIMEMO_PUBLIC_ORIGIN。变量缺失、为空或无效时必须失败；不得猜测服务器 IP、listen address、Host header 或 DNS 名称。

Non-interactive 不降低 collision、foreign state、direct exposure 或 destructive-action 保护。未来如需 destructive recovery，必须使用另行设计的显式命令；--non-interactive 本身不是同意删除或覆盖。

### 7.3 Listen

默认 listen 为：

    127.0.0.1:8088

安全 invariant 是 loopback by default，8088 不是 protocol。用户可以显式指定其他 loopback port。

若请求地址已经被占用，Installer 必须报告冲突及请求 endpoint，不得杀进程、抢占端口、自动选择随机端口、修改 firewall 或回退到 0.0.0.0。

非 loopback listen 只能由用户显式提供。Installer 必须在 mutation 前显示网络暴露警告；non-interactive 模式也必须在输出中保留警告。警告至少覆盖 HTTPS、Secure Cookie、OAuth/provider callback、Turnstile、firewall 和公网暴露责任。Installer 不得自动配置 firewall。

### 7.4 Canonical roots

Installer v1 只使用 Filesystem Layout v1 的五个 exact canonical roots。
`--app-root`、`--data-root`、环境变量 root override 与 custom deployment
profile 均不属于 v1 surface。

目标及其受管父目录不得是 symlink 或 junction。Installer 只能在经过分类的 AniMemo-owned empty directory 或已由 matching `v1.1-standard` instance locator 证明归属的 canonical directory 内写入。任何 custom、foreign 或 partial root 都必须在 mutation 前 fail closed。

## 8. Public Origin 输入

Public Origin 表示浏览器实际访问 AniMemo 的 canonical external origin，不表示 AniMemo 管理 DNS、TLS 或 proxy。

Interactive 安装必须提示管理员输入 Public Origin，并验证：

- scheme 为 http 或 https；
- 包含有效 host；
- 不含 path、query、fragment 或 userinfo；
- 规范化后只有一个 canonical origin。

Non-interactive 安装只接受显式 ANIMEMO_PUBLIC_ORIGIN。

Installer 必须使用 Public Origin 生成 application configuration 所需的 ALLOWED_HOSTS、CORS、CSRF 和 callback identity，但不得从 Public Origin 推导 listen address，也不得 hardcode animemo.cc。

DNS 解析、TLS handshake 和公网反向代理健康不是安装成功前置条件。Installer 可以提示管理员安装后检查它们，但不得自动配置 Cloudflare、DNS、证书、Nginx、OpenResty、Caddy、Traefik 或任何 hosting panel。

## 9. Precheck Contract

所有能在 mutation 前确定的失败必须先检查。Installer 不做过度自动修复；原则是 detect clearly、report clearly、fail safely。

| Precheck | 要求 | 失败行为 |
| --- | --- | --- |
| Argument contract | 参数完整、合法且无冲突 | Usage error；零 mutation |
| Privilege | 真实安装必须 effective UID 0；dry run 报告真实安装是否满足 | 缺少 root/sudo 时失败，不尝试提权 |
| OS | Linux | 不支持时失败 |
| Architecture | 当前 Manifest 只支持 linux/amd64；x86_64 必须规范化为 amd64 | 不匹配时失败，不尝试模拟 |
| Required tools | Docker daemon、Compose v2、HTTPS client 和所需 verifier 可执行 | 缺失或不可用时失败，不自动全局安装/升级 |
| Release network | 固定 GitHub API/Release assets、允许的 GitHub attestation bundle host 和 GHCR 可访问 | 明确区分 unavailable、rate-limited、authentication 和 verification failure |
| Release identity | 完整 trust chain 通过 | Authority error；零安装 mutation |
| Disk | app root、data root 和 Docker storage 的可用空间可确定且满足实现公布的 Installer v1 下限与 exact image需求 | 无法确定或不足时失败 |
| Filesystem | 父目录存在或可安全创建；ownership、permissions 和 filesystem type 可用 | 不递归修复未知内容 |
| Root safety | app/data/updater/state/runtime roots 不重叠、不为过宽目标、不经 link 跳转 | State conflict；零 mutation |
| Existing locator | instance.json 不存在或可解析且与请求一致 | 缺失/损坏/冲突按幂等矩阵失败 |
| Existing files | 每个目标分类为空、matching managed state、partial 或 foreign | Partial/foreign 不自动继续 |
| Existing data | data root 与 locator、Compose project 和数据库状态一致 | 无 locator 的数据不得自动 adopt |
| Compose identity | animemo project、container、network 和 volume 名称未被 foreign workload 占用 | 冲突时失败，不停止 foreign workload |
| Listen endpoint | 请求 address:port 可绑定，或由同一 matching instance 正常占用 | Foreign collision 失败 |
| Public Origin | canonical origin 语法有效 | Config error；不检查 DNS/TLS readiness |
| Systemd boundary | unit、sysusers、tmpfiles 和 allowlist 与 resolved roots 一致 | 不一致时 fail closed |

精确 Linux distribution 与 Docker/Compose 版本矩阵属于 Compatibility Matrix，延后到后续阶段。本 Contract 不承诺在未知发行版上自动安装系统依赖。

## 10. Instance discovery

固定 instance locator 为：

    /var/lib/animemo-updater/instance.json

它是 Installer、Updater、未来 Backup/Restore/Migration/Doctor 和 animemo CLI 发现 app/data roots 的 authoritative locator。

instance.json 必须：

- versioned；
- non-secret；
- 使用私有临时文件、fsync 和 atomic replace 写入；
- owner 为 animemo-updater，mode 0600，禁止 group/world-readable；
- 为单链接普通文件；
- 位于非 symlink 的 /var/lib/animemo-updater；
- 至少在语义上绑定 schema version、canonical app root、canonical data root、精确 `v1.1-standard` deployment profile、canonical listen、canonical Public Origin、managed config location 和 exact installed release identity；
- 不保存 credential、password、token、setup code、credential encryption key 或 provider secret。

实际 JSON schema、writer 和 reader implementation 延后，但实现不得通过解析 env、当前工作目录、Compose label、1Panel 路径或目录是否存在来覆盖 locator 中的 authoritative root。

若 locator、Compose mounts、Updater HostPaths、systemd ReadWritePaths/allowlist 或请求参数之间不一致，Installer 与 Updater 都必须 fail closed。不得选择“看起来存在”的目录继续运行。

locator 只证明已声明的 instance identity，不单独证明目录内容可信。所有 release/deployment bytes 仍必须进行 exact verification。

## 11. Idempotency 与 collision matrix

| 状态 | 识别条件 | Installer 行为 |
| --- | --- | --- |
| Fresh Install | locator 不存在；所有目标为空或可安全创建；无 foreign Compose/port/data | 完成 precheck 和 release verification 后安装 |
| Existing Compatible Install | locator 有效，roots/profile/Compose 一致，实例健康 | 不把 installer 当 updater；根据 exact version 进入下述 Same/Different 分支 |
| Already Installed Same Version | locator 与运行实例均绑定请求的 exact version、commit、API/Web digests 和 deployment identity | 健康检查通过则 no-op success；不得重跑 migration/bootstrap、轮换 secret 或重写数据 |
| Existing Different Version | matching instance 存在，但 exact release identity 不同 | 失败并提示使用 Updater；不得用 Installer 执行 upgrade/downgrade |
| Partial Install | state/app/data/systemd/Compose 只有一部分存在，或 locator 与实际内容不完整 | fail closed，报告已有与缺失组件；不得猜测 resume、repair 或 rollback |
| Foreign Files In Target Path | app/data/updater root 存在未知文件，或 owner/identity 不属于 matching instance | fail closed；不移动、不覆盖、不删除 |
| Existing Data Without Instance State | data root 含 PostgreSQL、Redis、media、plugin、backup 或 private state，但 locator 缺失 | fail closed；不得自动 adopt、初始化、迁移或 reset |
| Existing Instance | 检测到既有 AniMemo Compose、container、locator 或健康 endpoint，但请求 roots/profile 不一致 | fail closed；给出显式 cutover/Updater 路径 |
| Corrupt Locator | instance.json 非普通文件、权限错误、JSON/schema 无效或字段冲突 | fail closed；不得从 env 或目录结构重建 |
| Requested Port Used By Same Instance | locator、Compose 和健康响应均证明是 matching same-version instance | Same Version 分支；否则按 Existing Instance 失败 |
| Requested Port Used By Other Process | 无 matching instance proof | port conflict；不得杀进程或换端口 |

目录存在本身既不是 AniMemo ownership 证明，也不是删除授权。

Installer 永远不得：

- 因为目标目录存在而执行 rm -rf；
- 清空 PostgreSQL、Redis、media、plugins、backups 或 private；
- 自动执行 --reset-data 等价行为；
- 覆盖 unknown env 或 instance metadata；
- 为了“重装”重跑 migration；
- 把 health failure 当作许可重新初始化；
- 自动接管 foreign Compose project、container、network、volume 或 port。

失败后的 cleanup 只能删除本次执行创建且通过唯一 staging identity 证明归属的临时路径。任何持久数据或执行前已存在的文件都不能被 cleanup 删除。

## 12. Pre-production clean break

AniMemo 尚未投入生产。Installer v1 不包含 pre-v1.1 filesystem/config
reader、panel profile、custom-root profile 或 cutover operation，也不扫描已知
旧路径来判断 collision。

Existing state 只通过 fixed locator、canonical roots、Compose identity、port
ownership 与 exact release evidence 分类。无法形成 matching canonical instance
proof 的内容属于 Foreign、Partial 或 Existing Data Without Instance State；
Installer 必须 fail closed，且不得移动、覆盖、删除、adopt 或为其生成 locator。

该修正不放松 Data/Memory Integrity：unknown existing bytes 永远不是删除授权。

## 13. Transaction 与失败所有权

真实安装的顺序必须是：

    Parse and validate inputs
    → Classify existing state
    → Precheck platform/filesystem/port/dependencies
    → Resolve exact release
    → Verify full release trust chain
    → Stage AniMemo-owned bytes
    → Verify staged bytes against exact release identity
    → Create/configure AniMemo-owned runtime
    → Explicit migration
    → Explicit bootstrap
    → Start AniMemo-scoped services
    → Health check
    → Atomically publish instance locator

在 full release verification 前不得进行持久安装 mutation。

若 mutation 后失败，Installer 必须：

- 停止继续推进；
- 报告失败阶段和稳定 error class；
- 只回滚本次 staging 和安全可逆的 AniMemo-owned application changes；
- 保留数据库、media、plugins、backup 和任何执行前数据；
- 不自动 reverse migration 或 restore database；
- 若无法证明回滚安全，将状态标为 partial/manual recovery required，而不是伪装成功。

## 14. 错误与输出

Installer 至少应稳定区分：

| Error class | 含义 |
| --- | --- |
| usage_error | 参数缺失、非法或冲突 |
| unsupported_platform | OS/architecture 不受当前 release 支持 |
| dependency_unavailable | Docker、Compose、verifier 或必要 host capability 不可用 |
| release_unavailable | 固定 authority 暂时不可访问 |
| release_verification_failed | Manifest/checksum/tag/digest/deployment/provenance/attestation 不一致 |
| filesystem_conflict | root、permission、link 或 foreign file 冲突 |
| instance_conflict | locator、canonical profile、Compose 或 existing instance 不一致 |
| port_conflict | listen endpoint 被 foreign process 占用 |
| configuration_invalid | Public Origin 或 application config 无效 |
| partial_installation | 检测到未完成或不一致的受管状态 |
| health_check_failed | 服务已启动但健康门禁失败 |
| manual_recovery_required | mutation 已发生且无法安全自动回退 |

成功、same-version no-op 和 dry-run success 返回零。所有拒绝和失败返回非零。

错误输出必须说明：

- 哪个 invariant 失败；
- 哪个路径、port、release tag 或 contract field 发生冲突；
- 是否发生任何持久 mutation；
- 下一步是修正输入、使用 Updater，还是 manual recovery。

不得输出 env value、secret、token、credential、setup code、Authorization header、带 credential URL 或日志中的敏感内容。

## 15. 明确不支持的自动化

Installer v1 不提供也不隐式执行：

- --setup-nginx
- --setup-openresty
- --setup-caddy
- --setup-traefik
- --setup-cloudflare
- --setup-certbot
- DNS mutation
- TLS certificate issuance
- firewall mutation
- hosting panel integration
- global Docker prune
- Docker daemon restart
- shared PostgreSQL/Redis restart
- random port selection
- foreign process termination
- pre-v1.1 filesystem/config discovery、import 或 migration replay

历史 deploy/deploy.sh 的 panel/proxy bootstrap 行为不是 Installer v1 的实现基础、输入或 fallback。

## 16. Future interfaces

后续阶段依赖本文冻结的接口，但本阶段不实现：

- Backup Contract：从 instance.json 与 Filesystem Layout v1 发现 data root；不得把 app binaries 或 runtime socket 当作 instance backup。
- Restore Contract：验证 target roots 与 locator 状态；不得把 existing foreign data 当作空目标。
- Migration Bundle：携带 canonical app/data root 与 `v1.1-standard` profile metadata。
- Migration Secret Envelope：credential transport 另行设计，secret 永不进入 instance.json。
- Doctor Basic：读取 locator、listen、Public Origin、systemd/Compose alignment 和 exact release identity并进行只读诊断。
- animemo CLI：复用同一 locator 和 error classes，不自行猜测 roots。
- Updater：只从 validated canonical locator 发现新实例 roots，unknown profile fail closed。
- Compatibility Matrix：冻结精确 Linux distribution、Docker/Compose 版本和其他 host compatibility。
- Full Installer implementation：实现 install.sh、transaction、staging 和状态机。

以上全部为 DEFERRED。Installer Contract v1 的冻结不授权开始 Backup、Restore、Migration、Doctor、完整 CLI、Release 或 Production deployment。

## 17. Contract acceptance

## 18. Phase 3C implementation refinement

Phase 3C implements this contract behind one deep `Installer` Module. Its only
domain Interface is:

```text
plan(InstallRequest) -> InstallPlan
execute(InstallPlan, accepted_plan_digest) -> InstallResult
```

`InstallPlan` is canonical, machine-readable, non-secret, and binds operation
ID, mode, target snapshot, exact `VerifiedReleaseMaterials` aggregate identity,
qualified platform evidence, managed-config revision/projection, warnings, and
ordered steps. Execute rejects a changed Release, target, platform, config, or
Restore plan before mutation. CLI only parses/renders and calls this Interface.

Fresh execution uses the versioned `animemo.operation/v1` journal and fsyncs the
irreversible marker before database migration. It creates a new instance ID,
publishes protected config, stages exact material, runs explicit migration and
bootstrap jobs, proves exact running release, performs native Updater adoption,
publishes locator last, and requires complete Doctor acceptance. Failure after
the irreversible marker never reruns migration and returns durable
`manual_recovery_required`.

Restore-to-New is an Adapter over `prepare_restore` / `execute_restore`; it
preserves the backup instance ID and existing Backup, Secret Envelope, Resource
Budget, compatibility, recovery, and MI-1..MI-5 behavior. Installer does not
copy Restore/Migration validators or write Updater slots.

An exact healthy same-release invocation returns `NO_CHANGE` without rotating
secrets or mutating state. A healthy different-release invocation returns
`UPDATER_HANDOFF`. Foreign, partial, corrupt, data-without-locator, or conflicting
configuration fails closed. The stable exit classes distinguish success,
validation, compatibility, recovery, usage, and environment/tool failures.

The normal CLI requires explicit acceptance in the same invocation;
non-interactive execution requires `--accept` and never prompts. `--dry-run`
does not create roots, files, operations, containers, systemd state, or Docker
objects. Direct non-loopback listen remains an explicit warning-bearing choice.

只有以下条件同时满足，未来 Installer implementation 才可宣称符合 v1：

- install.animemo.cc 只运输 bootstrap；
- Release Authority 仍只有 GitHub Release + GHCR exact digest；
- stable/rc resolution 不使用 latest authority；
- 所有执行 bytes 绑定同一 exact release identity；
- authority 不完整时 fail closed；
- dry run 零持久 mutation；
- non-interactive 零 prompt；
- 默认 listen 为 loopback；
- roots 固定为 Filesystem Layout v1 canonical set，profile 精确为 `v1.1-standard`；
- same-version 是验证后的 no-op；
- partial、foreign、unknown data 零覆盖零删除；
- pre-v1.1 filesystem/config reader、custom profile、fallback 与 cutover 为零；
- DNS、TLS、proxy、firewall 和 hosting panel 不成为安装依赖；
- 未实现或未验证的未来能力保持 deferred。

任一条件不满足，Installer v1 acceptance 为 FAIL。
