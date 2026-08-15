# AniMemo Doctor Basic Contract v1

Status: FROZEN FOR v1.1

Version: v1

Scope: 冻结 AniMemo v1.1 最小、strict read-only 的结构诊断面、report schema、status aggregation、secret-safe evidence 与 failure ownership，使管理员能够回答“这个 instance 是否在结构上足够健康，可以运行或进入显式 repair？”

Definitions: Doctor Basic 是宿主机侧 diagnostic consumer；check 是一个有稳定 identity 的独立只读判定；report 是一次运行的 immutable 诊断结果；application checks 属于 AniMemo-owned boundary，external edge checks 属于 Administrator-owned boundary。

Non-goals: 不实现 Doctor runtime、repair、Installer、Backup、Restore、Migration、package install、service restart/reload、DNS/TLS/proxy mutation、credential rotation、database write、cache write、plugin activation、media write probe或 production monitoring daemon。

Dependencies: Doctor 读取 Phase 1 的 locator/filesystem/listen/Public Origin interfaces；compatibility outcome 只引用 Compatibility Matrix v1；backup readiness 只引用 Backup Contract v1；Doctor 不建立平行 compatibility 或 backup authority。

Security / Integrity implications: Doctor 可能以高权限读取 metadata，但只允许输出 secret-safe classification。默认严格 `READ-ONLY`，不得为了取得结果而改变 instance；raw exception、env/config value、credential、token、setup code、authorization data 与 decrypted secret 都不得进入 stdout、JSON、日志或 report。

Compatibility: `reportFormat` 与 `reportVersion` 独立于 application Release、Migration/Backup/Restore format 和 locator schema。Compatibility outcome 只允许 `COMPATIBLE`、`REQUIRES_UPGRADE`、`UNSUPPORTED`、`CORRUPT`；Doctor check status 则只允许 `PASS`、`WARN`、`FAIL`、`SKIPPED`。

Change policy: 改变 report/check schema、status aggregation、required check、read-only boundary、secret redaction、compatibility mapping 或 failure ownership属于 Contract 变更，必须记录兼容性并评审；stable checkId/code 不得重新赋予不同含义。

## Canonical contract set

Phase 1：

- [Deployment Boundary v1](deployment-boundary-v1.md)
- [Filesystem Layout v1](filesystem-layout-v1.md)
- [Installer Contract v1](installer-contract-v1.md)
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)

Phase 2：

- [Backup Contract v1](backup-contract-v1.md)
- [Restore Contract v1](restore-contract-v1.md)
- [Migration Secret Envelope v1](migration-secret-envelope-v1.md)
- [Compatibility Matrix v1](compatibility-matrix-v1.md)
- [Migration Bundle v1](migration-bundle-v1.md)
- [Doctor Basic Contract v1](doctor-basic-contract-v1.md)

## 1. Report identity

Canonical report identity：

```text
reportFormat: animemo-doctor-report
reportVersion: 1
```

顶层至少包含：

- `reportFormat`、`reportVersion`；
- `checkedAt`；
- non-secret `instanceId` when safely available；
- deployment profile；
- Doctor program exact release identity when available；
- `mode: READ-ONLY`；
- `overallStatus`；
- ordered `checks`；
- report schema/compatibility metadata。

Report 不得包含配置值、env dump、container environment、credential、secret、token、setup code、database row content、Authorization header、Cookie、signed URL 或 raw command output。

## 2. Per-check schema

每个 check 必须且只依赖稳定语义字段：

| FIELD | CONTRACT |
|---|---|
| `checkId` | stable dotted identifier；同一含义跨 patch 保持不变 |
| `status` | 仅 `PASS`、`WARN`、`FAIL`、`SKIPPED` |
| `code` | stable machine-readable code；不能使用 raw exception class/message |
| `severity` | `info`、`warning`、`error` 或 `critical` |
| `summary` | human-readable、redacted、bounded text |
| `evidenceClass` | `locator`、`filesystem`、`configuration`、`runtime`、`release`、`dependency`、`data-integrity` 或 `administrator-edge` |
| `remediation` | non-destructive next step 与明确 owner；不得伪装成自动 repair |
| `checkedAt` | UTC timestamp |

实现可以增加 bounded、non-secret evidence metadata，例如 path label、mode、UID/GID、free bytes、HTTP status、version/digest equality 或 duration；不得输出 env/config value。

### Status semantics

- `PASS`：该 check 的全部冻结 invariant 已验证成立。
- `WARN`：结构仍可运行，但存在明确、非破坏性的风险或后续动作。
- `FAIL`：冻结 invariant 已被证明不成立，或存在 integrity/security failure。
- `SKIPPED`：check 未执行；`code` 与 `summary` 必须说明 `unavailable`、`not_applicable`、`not_requested` 或 dependency deferred，绝不能输出 `UNKNOWN`。

`SKIPPED` 不等于 PASS。Required critical check 因 `unavailable` 被 SKIPPED 时，overall 必须 FAIL；warning check 被 SKIPPED 时 overall 至少 WARN；真正 not-applicable 的 optional check不降低 overall。

## 3. Overall status and exit behavior

Top-level status 只允许：

- `PASS`：所有 required checks PASS，optional skip 均为 not-applicable/not-requested；
- `WARN`：没有 required FAIL，但至少一个 required warning、warning-level unavailable skip 或 explicit administrator action；
- `FAIL`：任一 required check FAIL，或 required critical check unavailable/corrupt/unsupported。

推荐稳定 CLI exit contract：

```text
0 = overall PASS
1 = overall WARN
2 = overall FAIL
64 = usage/report-contract error
```

Doctor 不得因一个 check 抛出异常而丢失其他可安全执行的 check；应把该 check 转为 redacted FAIL/SKIPPED，再继续独立诊断。Report schema 本身无法生成时才使用 usage/report-contract error。

## 4. Strict READ-ONLY boundary

默认 Doctor Basic 永远不得：

- 写入、创建、chmod/chown、rename、unlink 或 cleanup instance path；
- 写入 `instance.json`、Updater runtime state、operation journal、cache、lock 或 socket；
- 自动迁移 legacy Updater state；
- 创建 database row、运行 migration、restore、bootstrap 或 rotate authentication epoch；
- 对 Redis 执行 `SET`、`DEL`、`FLUSH*` 或其他 mutation；
- activate、reload、disable、publish、rollback 或 cleanup plugin；
- 写入/删除 local media，枚举后删除 R2 object，或执行 orphan cleanup；
- 创建 backup 来证明 backup readiness；
- start/stop/restart/reload service、container、Docker daemon、proxy 或 firewall；
- 修改 Public Origin、listen、DNS、TLS、proxy 或 provider callback；
- 执行 repair、adopt、cutover 或 legacy data migration。

需要 mutation 的未来操作必须是单独命令、显式授权、独立 Contract；不得给 `doctor` 添加隐式 `--fix` 行为。

## 5. Discovery and fail-closed order

标准 v1.1 discovery：

1. lstat `/var/lib/animemo-updater/instance.json`，验证 regular single-link、owner/mode、非 symlink/junction、schema 和 canonical paths。
2. 读取 non-secret locator fields；secret 只验证配置文件 metadata/existence，不输出内容。
3. 对照 managed config location、app/data roots、Compose mounts、systemd allowlist 与 Updater profile。
4. 只有 alignment 成立后才运行 path-scoped runtime checks。

locator 缺失、损坏或与 config/Compose/systemd/Updater 不一致时不得从 env、当前目录、1Panel path、container label 或“看起来存在”的目录自动修复/覆盖。Doctor 可以继续执行不依赖 disputed root 的安全 checks，但 locator/alignment 必须 FAIL，overall 不得 PASS。

Legacy v1.0 profile 可以只读报告 `legacy_profile_detected` 与 `explicit_cutover_required`。Doctor 不生成 locator，不把 `/opt/animemo` 当第二实例，也不重跑 `/data/anime-journal` → `/data/animemo`。

## 6. Minimum check catalog

以下 checkId 为 Doctor Basic v1 的 minimum surface：

| CHECK ID | REQUIRED BEHAVIOR |
|---|---|
| `instance.locator` | 验证 locator path/type/schema/owner/mode、instanceId、roots/profile 与 non-secret rule |
| `filesystem.roots` | 验证五个 canonical/custom roots 的 canonicalization、存在性、类型、非 link 与不重叠 |
| `filesystem.permissions` | 按 Filesystem Layout 检查 owner/mode；不递归修复 |
| `filesystem.capacity` | 报告 app/data/updater state 与 Docker storage 可用空间；阈值由 Compatibility/Backup Contract 提供 |
| `configuration.required` | 仅报告 required config `EXISTS/MISSING/VALID/INVALID`；不输出 value |
| `configuration.alignment` | locator managed config、Compose、runtime contracts 与 systemd path一致 |
| `systemd.allowlist` | allowlist 不扩大到过宽父目录，并与 locator roots 精确一致 |
| `compose.alignment` | project/service、compose file identity、mounts、listen binding 与 locator一致 |
| `network.listen` | 验证配置 endpoint、loopback/default安全性、port ownership 与本地可达；不杀进程 |
| `identity.public-origin` | 验证 canonical origin 与 managed derived settings一致；不检查即修改 DNS/TLS |
| `database.postgresql.connectivity` | container health、`pg_isready` 与 read-only `SELECT 1`；不写数据 |
| `database.schema-compatibility` | read-only migration/schema contract inspection，并映射 Compatibility Matrix |
| `cache.redis.connectivity` | container health、`PING` 与 application client read-only connection；禁止测试写 |
| `cache.redis.persistence-contract` | 验证预期 volume/AOF/config；明确 Redis 是 persistent operational、rebuildable、非 authoritative memory |
| `service.api.health` | 通过配置 loopback、Public Origin Host/scheme 请求 API health；单独验证 HTTP 与 payload identity |
| `service.web.health` | 通过配置 loopback 请求 Web root/必要静态路由；不依赖公网 DNS 回环 |
| `updater.socket` | socket path/type/mode、服务可达性；不删除 stale/foreign path |
| `updater.state` | 只读 snapshot CURRENT/PREVIOUS/runtime contracts/PENDING/recovery block；禁止 refresh/migrate state |
| `release.identity` | locator ↔ verified Manifest ↔ deployment contract ↔ actual API/Web digest/labels ↔ API/Web health identity |
| `release.updater-consistency` | updater version/minimum、CURRENT/runtime contracts/enabled Plugin SDK APIs 与 running release一致 |
| `plugins.integrity` | DB deployment/manifest/CAS digest/runtime derivability/SDK compatibility；不加载或 activate plugin |
| `media.integrity` | stable MediaObject/backend references、local path/hash与 same-R2 identity；不删除 orphan |
| `backup.readiness` | 根据 Backup Contract 检查 root/mode、最近 artifact metadata/checksum/verification；不创建 backup |

实现可以添加 optional checks，但不得删除、合并或弱化 minimum check。一个聚合的“healthy”不能代替 PostgreSQL、Redis、API、Web、Updater 和 release identity 的独立结果。

## 7. PostgreSQL and Redis diagnostics

### PostgreSQL

Doctor 必须分层报告：

1. Compose/container state；
2. `pg_isready`；
3. application connection 的 read-only `SELECT 1`；
4. database/schema compatibility contract；
5. backup readiness separately。

Public `/health/` 返回 200 不证明 database 可用。Doctor 不执行 write probe、migration 或 restore。

### Redis

当前 Redis 使用持久 volume/AOF，但业务语义是 shared cache 与 fail-closed authentication throttle。Doctor 必须称其为：

```text
persistent operational state
+ rebuildable cache/security-throttle state
+ not authoritative AniMemo memory
```

Doctor 检查 container health、`PING`、application read-only connection、expected persistence config 和 filesystem boundary。它不能通过 cache `SET`/`GET` 证明连接，不能把 Redis key loss 报告为用户 memory corruption，也不能把当前未使用的 queue 语义写入 Contract。

## 8. API, Web and Public Origin

API `/health/` 当前可提供 status、release/artifact identity 与 database/configuration contract identity，但它不是 PostgreSQL/Redis deep health。Doctor 必须将 HTTP availability、payload validity、release equality 与 dependency connectivity 分开。

Local application check 使用 locator 中的 listen endpoint，并用 canonical Public Origin 派生 Host 与 forwarded scheme。不得依赖 public DNS loopback。Public Origin、listen 与 public edge 是三项独立 evidence：

- local listen/application health 属于 required AniMemo-owned checks；
- Public Origin config consistency 属于 required application identity check；
- DNS、TLS、public proxy、firewall 与 provider callback 是 Administrator-owned external checks。

Doctor Basic v1 默认不运行外部网络 checks。未来显式 external mode 必须使用独立 `administrator-edge.*` checkId，未请求时 `SKIPPED/not_requested`；external FAIL 不得伪装成 local application FAIL，也不能触发自动修复。

## 9. Updater diagnostics must be snapshot-only

Doctor 只允许读取已经存在的 Updater state snapshot。它不得：

- 调用会 refresh enabled plugin APIs 并写 `runtime.json` 的 status path；
- 调用会把 legacy CURRENT/PREVIOUS files迁移成新 envelope 的 reader；
- acquire/update lock、consume plan、recover operation、import CURRENT 或清理 socket；
- 通过 API/Django 向 Updater提供任意 host path；
- 在 locator/systemd mismatch 时放松 fail-closed behavior。

未来实现需要新增纯只读 snapshot interface，或使用明确保证 no-write/no-migrate 的 local reader。现有有副作用 API 即使名称为 `get_status` 也不是合格 Doctor probe。

Updater check 至少报告：service/socket availability、Updater exact version、CURRENT/PREVIOUS identity、runtime contracts、enabled Plugin SDK APIs、latest operation、PENDING/manual recovery block，以及与 running release/locator/systemd 的一致性。Operation detail 必须经过 redaction。

## 10. Release identity and compatibility

Doctor 必须独立比较：

- locator exact release identity；
- Updater CURRENT verified Manifest；
- local deployment contract files；
- Compose configured repository@digest；
- actual API/Web container image identity 与 immutable labels；
- Web effective release identity；
- API health release/artifact/database/configuration identity；
- Updater minimum/version 与 enabled Plugin SDK APIs。

只比较 SemVer 或 `/health/` version 不足以 PASS。Bundle、cache、mutable tag、registry latest 或 HTML 页面不是 Release Authority。

Compatibility dimension 只使用以下 outcome，并映射为 Doctor status：

| COMPATIBILITY | DOCTOR STATUS |
|---|---|
| `COMPATIBLE` | PASS |
| `REQUIRES_UPGRADE` | WARN；只提示显式 Updater/contract path，不执行 upgrade |
| `UNSUPPORTED` | FAIL |
| `CORRUPT` | FAIL |

Unknown incompatible major/critical extension 必须 `UNSUPPORTED`；integrity failure 必须 `CORRUPT`。Doctor 不发明其他 compatibility value。

## 11. Plugin and media diagnostics

### Plugins

Doctor 只读 cross-check：

- PluginPackageBlob digest/storage path 与 canonical CAS file；
- current/previous PluginVersion、Manifest snapshot 与 deployment rows；
- UserPluginInstallation 和 PluginData 存在性/关系完整性；
- enabled SDK APIs 是否被 current release接受；
- runtime directory 是否可由 verified CAS 重建。

Doctor 不调用会 activate/unload runtime 的 plugin discovery，不执行 cleanup/GC，不删除 orphan CAS，不因插件不兼容丢弃 PluginData。Missing authoritative CAS 或 DB/file mismatch 是 FAIL，不是自动停用后 PASS。

### Media

Doctor 只读 cross-check stable MediaObject reference、backend physical identity、object key、size/hash metadata、local file 和 pending reservation。Remote backend 默认只验证配置与已知 managed identity，不枚举 whole bucket，不下载所有 object，也不删除 unknown orphan。

External provider metadata/R2 暂时不可用只能 WARN/FAIL 对应的 dependency check；不得删除 Journal、poster reference 或其他 memory。

## 12. Backup readiness

Doctor 的 `backup.readiness` 服从 Backup Contract v1。它可以只读验证：

- backup root type、owner/mode、非 link 与 declared capacity；
- latest artifact/metadata 是否存在；
- manifest/checksum/gzip 与 compatibility metadata是否可验证；
- freshness 与 retention warning；
- database logical backup principle。

Doctor 不创建“测试 backup”、不写 probe file、不清理旧 backup，也不能把 Updater pre-migration database dump 单独宣称为完整 instance backup。

## 13. Configuration and secret-safe output

Config diagnosis 只允许输出：

```text
EXISTS / MISSING
VALID / INVALID
AUTH PASS / AUTH FAIL
CONSISTENT / INCONSISTENT
```

不得报告 secret 长度、hash、fingerprint、prefix/suffix 或可逆 fragment。Doctor 不输出 `.env` 行、parsed connection URL、database host credential、provider credential、cookie、setup code、secret key 或 migration passphrase。

所有 command/library exception 必须映射成 stable code 与 bounded redacted summary。Raw `str(error)`、stderr/stdout dump 和 container environment禁止进入 report。

## 14. Memory Integrity invariants

- **MI-1 External metadata loss never deletes memory.** Doctor 只报告 provider/media metadata unavailable，不删除或建议自动删除 user memory。
- **MI-2 Provider identity change never silently orphans memory.** Identity mismatch 是 FAIL/WARN evidence；Doctor 不自动 rewrite identity。
- **MI-3 Identity merge retains historical references.** Doctor 可检测 merge/conflict，但 repair 必须保留历史 reference；Basic 不执行 merge。
- **MI-4 Unsupported memory is never silently discarded.** Unknown Core/plugin/provider/media extension 映射 `UNSUPPORTED`，不得通过忽略字段获得 PASS。
- **MI-5 Destructive ambiguity fails closed or requires explicit repair.** Locator/root ownership、duplicate instance、missing CAS/media 或 unknown orphan 不允许自动 adopt/delete；Doctor 只给 explicit future repair remediation。

## 15. Failure ownership and remediation

| FAILURE | CHECK OWNER | DOCTOR BEHAVIOR |
|---|---|---|
| locator/filesystem/config/Compose/systemd/release mismatch | AniMemo | FAIL，保持零 mutation，建议显式 Installer/Updater/cutover/repair path |
| PostgreSQL/Redis/API/Web/Updater local failure | AniMemo | 分组件 `FAIL` 或 `SKIPPED/unavailable` evidence，不全局 restart |
| disk/backup risk | Shared | WARN/FAIL；管理员提供容量，AniMemo提供 scoped retention/backup interface |
| invalid Public Origin/listen | Shared | FAIL application config；不修改 DNS/proxy/firewall |
| DNS/TLS/public proxy/firewall failure | Administrator | 独立 external check WARN/FAIL；不影响已通过的 local check，也不自动修复 |
| provider callback/credential unavailable | Shared | redacted WARN/FAIL；管理员处理 provider侧，AniMemo保持 memory/reference |

Remediation 必须说明 ownership 和下一步，但不得输出 shell command 去删除数据、绕过 verifier、修改 shared VPS 或放宽 listen/updater fail-closed。

## 16. Current → Target gaps

| AREA | CURRENT | TARGET | CLASSIFICATION |
|---|---|---|---|
| Phase 1 diagnostic boundary | roots、locator、listen/origin 与 Doctor expectation 已冻结 | 直接复用 | ALREADY SATISFIED |
| API health identity | `/health/` 已返回 release/artifact/contracts | 明确仅是 API/identity，不冒充 DB/Redis health | ALREADY SATISFIED |
| Compose/smoke probes | 已有 container、PostgreSQL、Redis、API/Web building blocks；部分 smoke 会写 cache/media | Doctor 仅复用严格只读 probe | DOCUMENTATION GAP |
| Staff system health | 现有 endpoint 不是 Doctor，且 raw error/plugin discovery可有副作用 | 新增 stable、redacted、snapshot-only checks | IMPLEMENTATION DEFERRED |
| Updater status | `get_status` 可 refresh/write runtime state；legacy slot read 可 migration | 新增 pure read-only/no-migrate snapshot interface | IMPLEMENTATION DEFERRED |
| Release verification | Updater 已能核对 Manifest、digest、labels、API/Web identity | 提取 read-only diagnostic adapter | ALREADY SATISFIED |
| Locator/config runtime | Phase 1 schema/interface存在，reader/writer与 managed config 未实现 | Doctor只读 consumer | IMPLEMENTATION DEFERRED |
| Report schema | 无 canonical report/check/status/exit contract | 本文件冻结 | DOCUMENTATION GAP |
| Compatibility/backup readiness | #90/#89 是唯一 cross-cutting authority | Doctor引用，不自建 | DOCUMENTATION GAP |
| Standalone Doctor bytes | 当前无 exact-bound standalone host program | 若新增 Release asset/program，先建立 exact byte binding | RELEASE CONTRACT REVIEW NEEDED |

## 17. Acceptance and STOP

Doctor Basic v1 只有在以下全部成立时才符合：report identity/version 正确；check status 只使用 PASS/WARN/FAIL/SKIPPED；overall 聚合确定；所有 minimum checks独立；默认 READ-ONLY；无 side-effect probe；无 secret/raw exception；compatibility/backup authority不重复；local与administrator-edge分离；Updater/locator mismatch fail closed；MI-1..MI-5 全部满足。

出现任何需要写 state 才能“诊断”、需要 relax Updater fail-closed、需要显示 secret、需要调用 plugin/media/cache mutation、需要自动 adopt/repair、需要把 external edge failure代替 local application health，或需要用 `UNKNOWN` 隐藏 unavailable check 时，必须 STOP。

本 Contract 冻结诊断语义，不授权实现 runtime、repair、Release、Deployment 或 Production operation。
