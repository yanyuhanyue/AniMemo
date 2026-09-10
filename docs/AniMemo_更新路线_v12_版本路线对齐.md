# AniMemo 完整 Master Roadmap v12：版本路线对齐

更新：2026-09-09。状态：ACTIVE ROADMAP（基于本次明确接受的映射）。
本文件是 README 指向的当前完整路线；历史 v10 原文保持不变。
本次只调整未发布里程碑编号与已证实状态，不增加平台范围或提前生产资格。

Memory must survive time, and the system holding that memory must survive time too.

AniMemo 是一个以时间为轴，保存用户与动画之间长期关系的个人动漫记忆系统。

AniMemo 要解决两个问题：让用户多年以后仍能找回自己与动画之间的记忆；也让多年以后仍然能够轻松维护保存这些记忆的 AniMemo 实例。

下面这版将此前已接受路线与本次第一方官方扩展专项全部收口，包括：

- **Memory Axis / Durability Axis / Quality 横轴**
- Panel-independent Deployment Boundary
- Installer / Backup / Restore / Migration
- UI/UX 2.0
- UI Platform & Memory Editorial Design System
- Character Memory
- Memory Anchor
- `occurred_at / recorded_at / time_precision`
- Revision / History
- Memory Thread
- Memory Integrity
- Portable Data
- Poster Pipeline
- Plugin Compatibility Horizontal Track
- Plugin Runtime v3
- Plugin Lifecycle & Operations
- Long-term Self-host
- AI 年度总结
- Memory Graph
- Rediscovery / 推荐算法
- PWA / Mobile
- Roadmap Freeze Rule
- Session Secret Broker / Unified Credential Lease
- Release Portable Two-layer Identity 与三份正式 Receipt
- Pre-Publish Trust Path Shadow Gate
- VM Profile Timing / Network Transport / Resource Lease
- install.animemo.cc 分阶段演进
- Deprecated Feature / Legacy Code 分波次退役
- Roadmap Full Markdown Artifact Rule
- Base UI单一Primitive、shadcn本地源码与Tailwind CSS 4
- CSS → Motion → GSAP动画所有权
- Memory Editorial / Cinematic / Calm Utility视觉语言
- Homepage Public Visibility / Authenticated Personal Entry / Cross-user Race Closure
- First-party Official Extensions / Core Boundary

并且不再继续给 v2.2 横向塞新概念。


---

## 路线权威、当前状态与版本解释（v12 修订）

**文档状态：** MASTER ROADMAP v12 / ACTIVE ROADMAP
**继承基础：** 已批准 Active v10 全文（SHA256 `6777591e0ea218b98d50cd087fc02699e4fe61564911138a657b4f691eca2824`）；保留 Memory、Durability、Quality、第一方扩展、UI、安装入口及生产准备范围。v11 候选未激活。
**本次决定：** 2026-09-09 启动指令接受下表映射。版本号是里程碑计划属性，未来真实破坏已发布公共合同仍须选择 major。
**当前源码：** A03 #229、A07 #231、CodeQL #232、R01 #233、gRPC #230 已合并；本次基线 main `780505b72ce15b2e55104de142888d46c8946713`。这些任务不重开。
**当前准备阶段：** 版本路线对齐、Guest Authority 离线修复与审查；真实 VM、Qualification、Candidate、Freshness、RC、Formal、Stable 另需明确授权。旧 RC19 与旧 Qualification 保留历史身份，不能用于新 main。
**生产策略：** `v2.0.0` 和计划 `v2.1.0` 是 `PREPRODUCTION_ONLY`；`MEMORY_PRODUCTION` 计划 `v2.2.0` 才是首个正式生产版本，原生产准入范围和质量门不变。
**Clean Break 策略：** 首次正式生产前允许修正错误边界，保护真实数据、Memory/Resource Identity、Backup、Migration、Release Authority；未来破坏已发布公共合同须诚实按 SemVer 升级。

| 稳定里程碑 ID | 原计划 | 新计划属性 | 范围／依赖 |
|---|---|---|---|
| HISTORICAL_RELEASES | v1.0.0、已发布 v1.x RC | 保留原号 | 已发布历史身份 |
| DURABLE_DEPLOYMENT_REFERENCE | v1.1.0 | v2.0.0 | PREPRODUCTION_ONLY 部署参考 Stable |
| PLATFORM_FOUNDATION | v1.1.x | 部署参考 Stable 后的 PF1～PF6 / W1～W7 波次 | 不指定 patch；按真实变更选择发行号 |
| WEB_PLATFORM | v1.2.0 | 计划 v2.1.0 | PREPRODUCTION_ONLY Web/UIUX |
| HOST_AGENT / PLUGIN_BOUNDARY | v1.2.1 / v1.2.2 | Web 后、生产前的独立波次 | 保持 Agent → 插件边界依赖，不硬塞 patch |
| MEMORY_BUILD / PRODUCTION_READINESS | v1.3-alpha / beta | 计划 v2.2-alpha / beta | 原范围、数据与运行条件保留 |
| MEMORY_PRODUCTION | v1.3.0 | 计划 v2.2.0 | FIRST_FORMAL_PRODUCTION_VERSION |
| SAFE_EXPANSION | v1.4 | 计划 v2.3 | 按需求安全扩展 |
| LONG_TERM_SELF_HOST | v1.5 | 计划 v2.4 | 长期运维 |
| MEMORY_INTELLIGENCE | v1.6+ | 计划 v2.5+ | 需求驱动 |

PF1～PF6、W1～W7 是工作包标识，不是版本号。本文保留的 `V1_*` 历史任务 ID 不改名；API v1、Plugin SDK v2、Runtime v3、schema/Contract v1/v2 与 `v1.1-instance-scoped` 等 profile 有独立身份。下文 v2.1 及之后的数字均为计划，不能代替当时 SemVer 决策。

部署参考 Stable 表示不可变且可验证的发行物；它不表示已适合长期生产数据。所有后续平台实现仍以部署参考 Stable 完成和获准执行为前提。

### v12 维护的横向主线

v5 已包含插件平台横向子路线，但还需要同时维护以下横向主线：

```text
W — Release Workflow & Developer Throughput
    门禁前移、风险分类、性能分档、VM 并行、Build-once、证据复用

R — Runtime & Data Authority
    PostgreSQL Durable Jobs / Outbox、Python Worker、Redis 非权威、容器数据权威

O — Observability & Recovery
    结构化日志、稳定错误码、Trace Context、Core Metrics、灾难恢复

A — Host Agent & Operations
    Python Agent 合同冻结、Go Shadow、Single Writer 切换、Doctor、Update/Backup/Restore

S — Security & Secret Lifecycle
    Code Scanning 治理、异常边界、Session Secret Broker、权限与供应链

P — Plugin Platform
    SDK v2 兼容、Trusted Capability、低权限 Supervisor、Runtime v3、Lifecycle

M — Memory Product Contracts
    Long-running Series、Memory Moment、Yearly Revision、Achievement & Badge

E — Open-source Engine Adoption
    Adapter/Wrapper、ADR、依赖分级、压缩归档、age、替换与退出计划

U — UI Platform & Visual Language
    Tailwind 4、Base UI 单一 Primitive、shadcn 本地源码、Design Tokens、Motion Policy、Memory Editorial

H — Homepage Identity & Privacy
    显式公共Owner、PUBLIC-only、公共有界读取、个人端点、最小DTO、no-store、模式与竞态闭合

C — Code Lifecycle
    Legacy/Deprecated/Dead Code 盘点、Reachability、分批删除、兼容层退役
```

所有横向主线服从同一优先级：

```text
Data / Memory Integrity
>
Security / Release Authority
>
可恢复性与长期维护
>
时间成本优化
>
功能与视觉扩展
```


## 路线图更新与完整 Markdown 产物治理（v9 继承）

以后任何涉及以下内容的讨论，都视为“路线图更新”：

```text
新增版本任务
调整版本归属
新增横向主线
修改合同名称
新增或删除 Gate
改变依赖顺序
推迟或提前功能
增加长期不变量
修改首个生产版本条件
```

每次路线图更新必须执行完整闭环：

```text
读取最新权威 Master Roadmap
→ 合并本次新增/修改内容
→ 重新检查历史已接受合同是否仍存在
→ 更新版本地图、DoD、合同索引和路线覆盖矩阵
→ 生成一份完整、可独立阅读的 Markdown Master Roadmap
→ 在回复结尾提供该完整 .md 文件
```

禁止只输出：

```text
局部补丁
一段追加文字
只有差异没有完整正文
只在聊天中口头确认
```

正式产物规则：

- 完整路线文件命名为 `AniMemo_更新路线_v<N>_<摘要>.md`；
- 新文件继承上一权威版本全部仍有效内容，不覆盖旧版本；
- 可额外生成 patch、缺口矩阵、SHA256 和 ZIP，但它们不能替代完整 `.md`；
- 每次更新必须在文档顶部更新版本、状态、继承基础和变更摘要；
- 每次更新必须在文档末尾更新“路线覆盖自检”；
- 未进入完整 Markdown 的口头设计不视为已纳入 Active Roadmap；
- 当前对话与以后所有对话都执行该规则。

固定门禁：

```text
ROADMAP_UPDATE_COMPLETE_MARKDOWN_REQUIRED=YES
ROADMAP_UPDATE_PARTIAL_ONLY_ALLOWED=NO
ROADMAP_PREVIOUS_ACCEPTED_ITEM_LOSS_COUNT=0
ROADMAP_VERSION_MAP_UPDATED=YES
ROADMAP_DOD_UPDATED=YES
ROADMAP_CONTRACT_INDEX_UPDATED=YES
```

---

# AniMemo 最终长期路线图 v12

## North Star

> **AniMemo 要让用户多年以后仍能找回自己与动画之间的记忆，也让多年以后仍然能够轻松维护保存这些记忆的 AniMemo 实例。**

因此 AniMemo 长期只有两条真正的主轴：

```text
                           AniMemo
                              │
              ┌───────────────┴───────────────┐
              │                               │
         MEMORY AXIS                    DURABILITY AXIS
              │                               │
      记忆是否真正留下来                承载记忆的系统能否活下去
              │                               │
      Timeline / Memo                    Installer
      Character Memory                   Updater
      History                            Backup
      Collections                        Restore
      Yearly Memory                      Migration
      Search                             Doctor
      Memory Engine                      Compatibility
      Rediscovery                        Release Trust
```

同时有一条贯穿所有版本的：

```text
QUALITY

Security
Privacy
Accessibility
Performance
Compatibility
Data Integrity
Memory Integrity
Release Engineering
Documentation
```

其中 **Memory Integrity** 正式成为长期原则：

> **外部 Metadata 可以消失，Provider 可以变化，插件可以卸载，服务器可以迁移，但用户自己产生的 Memory 不得因此静默消失、失去引用或被覆盖。**

------

# 历史基线：v1.0.0 — Reliable Foundation（已完成）

## 目标

> **先证明今天保存进去的数据能够安全、可靠地活下来。**

该历史阶段完成：

```text
Final RC
↓
Release Authority
↓
Fresh Lab
↓
Upgrade Lab
↓
Production Acceptance
↓
Production Smoke
↓
same artifact promotion
↓
v1.0.0 Stable
```

保持：

```text
Accepted RC API digest
=
Stable API digest

Accepted RC Web digest
=
Stable Web digest
```

不重新 Build Stable。

该历史阶段不加入：

```text
× Installer
× UI/UX 大改
× Timeline
× Character Memory
× Poster Pipeline
× Runtime v3
× PWA
× AI
× 推荐系统
```

### v1.0 Gate

> **这套 Release / Upgrade / Production 基础，是否已经值得用户把真实长期数据交给它？**

必须回答“是”。

------

# v1.0.x — Stability & Security

定位：

> **保护已经发布的 AniMemo，而不是扩功能。**

允许：

```text
Bug Fix
Security Fix
Data Integrity Fix
Updater Fix
Backup Fix
Deployment Fix
Compatibility Fix
Observability
低风险性能优化
```

不允许：

```text
大 UI 重构
新 Memory 数据模型
Installer 大改
Plugin Runtime
新平台级能力
```

------

# v2.0 — Durable Deployment


## v2.0.0 发行准备边界

当前只完成版本路线对齐与 Guest Authority 的离线实现、测试和独立审查。后续执行需要新的明确授权，并按以下顺序绑定真实权威：

```text
最终 exact main 与受审控制器
→ 新 Qualification
→ Candidate Acceptance
→ Trusted Metadata Freshness
→ Immutable RC Publish
→ R2 Mirror
→ Formal Three-profile VM
→ Private Canary / Stable 合同
→ v2.0.0 Durable Deployment Reference Stable（PREPRODUCTION_ONLY）
```

历史 v1.1.0 RC19 及旧 run33627874404 保留原身份，不自动覆盖本次源码。A03/A07/R01/CodeQL/gRPC 已合并，不重开。平台工作流、通用 Worker、Go Agent、Plugin Runtime、UIUX 与 Memory 后续范围仍按下文波次实施，不混入本次发行准备。

部署参考 Stable 完成后，按独立授权启动 PLATFORM_FOUNDATION。源码或 workflow 变化必须重新判断 exact-source 资格；版本数字不能把旧 Receipt 提升为新权威。

核心问题：

> **AniMemo 是否已经好安装、好备份、好迁移、好恢复，并且完全不依赖某个面板？**

上传路线分析本身也已经指出，Deployment 应该从原先过大的 v1.1 规划（部署职责现由 v2.0.0 承接） 中独立出来。

------

## v2.0-A — Panel-independent Core

彻底移除类似：

```text
/opt/1panel/docker/compose/animemo/app
```

这样的核心生产路径绑定。

标准化：

```text
/opt/animemo/
├── app/
├── deploy/
└── config/

/data/animemo/
├── postgres/
├── redis/
├── plugins/
├── logs/
├── backups/
├── media/
└── private/

/opt/animemo-updater/

/var/lib/animemo-updater/

/run/animemo-updater/
```

------

## Deployment Boundary v1

AniMemo 管理：

```text
自己的目录
自己的 Docker Compose project
PostgreSQL
Redis
API
Web
Updater
Public Origin
local listen endpoint
```

AniMemo **不管理**：

```text
DNS
TLS
Let's Encrypt
80/443
Nginx
Caddy
OpenResty
Traefik
Cloudflare
Cloudflare Tunnel
1Panel
宝塔
aaPanel
```

最终模型：

```text
Internet
   │
用户自己的 DNS / HTTPS / Reverse Proxy
   │
   ▼
127.0.0.1:<AniMemo Port>
   │
   ▼
AniMemo
```

以后不开发：

```text
1PanelAdapter
BTPanelAdapter
aaPanelAdapter
```

只提供配置文档。

------

## Configurable Loopback Endpoint

默认：

```text
127.0.0.1:8088
```

但正式 Contract 是：

> **默认只绑定 loopback。**

而不是：

> 必须使用 8088。

支持：

```bash
animemo config listen 127.0.0.1:18088
```

这也符合前面对 8088 应为默认值、而非硬协议的建议。

------

## Official Installer

入口：

```bash
curl -fsSLo /tmp/animemo-install.sh \
  https://install.animemo.cc/install.sh

sudo sh /tmp/animemo-install.sh
```

Installer 负责：

```text
Preflight
↓
Docker environment
↓
AniMemo directories
↓
Permissions
↓
Release verification
↓
PostgreSQL
↓
Redis
↓
API/Web
↓
Updater
↓
Migration
↓
Bootstrap
↓
Health
```

到这里结束。

不碰：

```text
DNS
TLS
Reverse Proxy
Panel
```

------

## Installer Trust Contract

正式冻结：

```text
install.animemo.cc
=
Bootstrap Transport

NOT
Release Authority
```

Installer 必须验证：

```text
Release identity
Source SHA
Manifest
Checksums
Deployment Contract
OCI digest
Provenance / Attestation
Installer compatibility
```

之后才能：

```text
pull repository@sha256:...
```

绝不能：

```text
docker pull latest
```

这一点应该属于 Installer P0。

------

## Installer Contract v1

逐步支持：

```text
--channel stable
--channel rc
--version
--dry-run
--non-interactive
--listen
--app-root
--data-root
```

默认永远：

```text
stable
```

------

## Public Origin

Installer 可以问：

```text
https://anime.example.com
```

但只用于：

```text
ANIMEMO_PUBLIC_ORIGIN
ALLOWED_HOSTS
CORS
CSRF
derived callbacks
```

不用于 TLS/DNS。

允许：

```text
Configure now
或
Configure later
```

------

## Domain Management

以后：

```bash
animemo config domain https://new.example.com
```

只负责：

```text
parse
↓
validate
↓
candidate config
↓
ALLOWED_HOSTS
↓
CORS
↓
CSRF
↓
callback derivation
↓
atomic write
↓
AniMemo scoped reload
↓
health
```

用户自己负责：

```text
DNS
TLS
Reverse Proxy
```

------

## Backup Contract v1

```bash
animemo backup
```

数据库使用：

```text
pg_dump
↓
compression
↓
SHA256
↓
metadata
```

绝不以 live PostgreSQL data directory tar 作为正式备份方式。

Backup Manifest：

```text
backup_schema_version
source_animemo_version
source_commit
database_schema_version
postgres_version
plugin_contract_version
plugin_inventory
media_backend_inventory
created_at
checksum
```

Restore 必须首先生成 Compatibility Plan。

------

## Restore Contract

```text
Backup
↓
Verify
↓
Compatibility Plan
↓
Explicit confirmation
↓
Restore
↓
Health
```

状态：

```text
COMPATIBLE
REQUIRES_UPGRADE
UNSUPPORTED
CORRUPT
```

------

## Migration

```bash
animemo backup --migration
```

然后：

```text
Old Server
↓
Migration Package
↓
New Server
↓
Install AniMemo
↓
Restore
↓
Verify
```

R2 对象原则上不迁移。

恢复：

```text
MediaObject identity
object key
backend identity
credentials
```

继续使用原 Bucket。

------

## Migration Secret Envelope v1

正式处理：

```text
CREDENTIAL_ENCRYPTION_KEY
DJANGO_SECRET_KEY
necessary instance secrets
```

独立：

```text
migration passphrase
+
KDF
+
authenticated encryption
```

不能用 `CREDENTIAL_ENCRYPTION_KEY` 加密一个包含它自己的 Migration Bundle。

------

## Doctor Basic

```bash
animemo doctor
```

至少检查：

```text
OS
Architecture
Docker
Compose
Disk
Memory
Ports
Permissions

PostgreSQL
Redis
API
Web
Updater

CURRENT
Release Identity
Backup
Listen Address
Public Origin
```

------

## Compatibility Matrix

建立明确支持范围：

```text
OS
Architecture
Docker
PostgreSQL
Redis
Browser
Upgrade Window
Plugin SDK
Backup Schema
Installer Contract
```

Installer 出现以后，这个必须正式维护。

### v2.0 Gate

> 一个不知道 AniMemo 内部实现的管理员，能否完成安装、更新前检查、备份、恢复、迁移和换域名？

并且：

> 宝塔 → 1Panel 是否已经完全不等于“迁移 AniMemo”？

------



# 部署参考 Stable 后的平台基础波次 — Preproduction Platform Foundation

定位：

> **在不承担正式生产兼容压力的窗口内，降低发行成本、建立持久任务和权威边界，为 v2.2 首次生产消除结构性返工。**

这条路线从 v2.0.0 Stable 后立即启动，并贯穿 PF1～PF6。它不是普通“技术债清理”，而是第一次正式生产前的基础平台工程。

## 平台基础 W 横向主线 — 工作流与时间成本优化横向主线

核心原则：

```text
把便宜、确定性的失败前移
→ 按可信风险分类选择门禁
→ 将完整 25 分钟性能证明移出普通路径
→ 独立 Gate 和 VM 并行
→ 同一候选字节只构建一次
→ 仅复用 exact-tree 等价证据
→ 不削弱最终 Stable 权威证明
```

### 平台基础 PF1-W1 — 计时、风险分类与早期失败

正式任务：

```text
V1_1_1_GATE_TIMING_INSTRUMENTATION_AND_RISK_CLASSIFICATION_V1
```

实现：

- 每个 Job、Step、Artifact、VM Profile 的开始/结束时间和等待时间；
- 单一 changed-files / risk classifier，输出闭合 Receipt；
- 风险类别：Metadata、Frontend、Application、Data Migration、Platform、Supply Chain、Security Core；
- G0 在 1～2 分钟内完成 Release Notes、Schema、YAML、Shell、权限、DAG、Artifact 命名等确定性检查；
- 普通 PR `cancel-in-progress=true`，Trusted Pre-Merge、Qualification、Publish 等事务继续禁止自动取消；
- Release Metadata Preflight 与 Producer Runtime Readiness 早于 Docker/OCI/性能；
- Artifact retention 与失败 forensic 保留策略；
- Workflow DAG 静态验证，禁止循环授权、不可达 Gate 和多 mutation owner；
- 每个 skip 必须记录 `NOT_REQUIRED_BY_SIGNED_RISK_CLASSIFICATION`，禁止无解释 `if: false`。


#### VM Profile Timing Receipt（正式合同）

新增：

```text
animemo.vm-profile-timing-receipt/v1
```

每个 Candidate / Formal Profile 都必须独立生成，至少记录：

```text
acceptance_session_id
profile_execution_id
profile
candidate_or_release_identity
base_vm_identity
clone_started_at / clone_duration
boot_started_at / boot_duration
provider_readiness_duration
material_staging_bytes / duration
network_download_bytes / duration
platform_bootstrap_duration
installer_duration
doctor_duration
canonical_test_duration
shutdown_duration
cleanup_duration
wall_clock_duration
critical_path_stage
selected_transport_path
proxy_selection_receipt_id
resource_lease_id
result
```

原则：

- Timing Receipt 是 `ENVIRONMENT_BOUND` 非权威执行证据，不改变 Candidate / Release Identity；
- 不记录 Secret、完整 URL、私人主机坐标或命令行凭据；
- 所有阶段必须使用单调时钟计算 duration，墙钟只用于审计时间戳；
- Profile 失败也必须生成 partial timing receipt；
- Aggregate Receipt 只引用 Timing Receipt 摘要，不复制大段遥测；
- 连续至少 10 轮后，才允许以数据决定并发度、代理策略、linked clone 或缓存优化。

正式任务：

```text
V1_1_1_VM_PROFILE_TIMING_RECEIPT_AND_CRITICAL_PATH_BASELINE_V1
```

### 平台基础 PF1-W2 — Performance Smoke / Full 与验收并行

正式任务：

```text
V1_1_1_PERFORMANCE_SMOKE_FULL_SPLIT_AND_PARALLEL_ACCEPTANCE_V1
```

实现：

```text
Performance Smoke
=
普通 PR / Patch 的 2～5 分钟快速回归

Performance Full
=
RC Qualification、Stable、性能相关或高风险变更的 1,500 秒完整证明
```

同时完成：

- 三套 Candidate Profile 独立执行；
- 非共享根因下，一个 Profile 失败不能默认阻止另外两个；
- Aggregate Receipt 能表达 `PASS / FAIL / NOT_RUN_SHARED_BLOCKER`；
- Candidate 与 Formal 分别保留产品字节验证和发布后真实信任链验证，不混为同一 Gate；
- Formal 独有 Sigstore/Provenance 路径使用固定 Actions Provenance Fixture 在发布前调用真实 verifier；
- 变更影响矩阵决定重跑范围，工具/文档局部变更不再无条件触发全部 Profile。


#### Pre-Publish Trust Path Shadow Gate（发布前信任路径影子门禁）

新增正式 Gate：

```text
PRE_PUBLISH_TRUST_PATH_SHADOW_GATE_V1
```

它必须在任何不可变 Tag、GitHub Release、GHCR push、Attestation 发布或 R2 Mirror 写入之前执行：

```text
固定 GitHub Actions provenance fixture
→ production provenance loader
→ canonical Sigstore policy builder
→ real sigstore-go verification
→ frozen release verifier
→ exact expected subject / issuer / repository / workflow / ref / SHA checks
```

必须覆盖正向与负向 fixture：

```text
wrong issuer
issuer duplicated in matcher and extensions
wrong repository
wrong workflow
wrong ref
wrong source SHA
wrong subject
missing extension
malformed bundle
untrusted chain
local transport basename differs from canonical subject
```

边界：

- Shadow Gate 使用固定 fixture，证明发布后独有代码路径在发布前可执行；
- Shadow Gate Receipt 不授予 Release Authority，不替代发布后的真实 Actions provenance、Sigstore证书和Formal VM；
- production loader、policy builder、sigstore-go和frozen verifier必须是真实生产调用面，禁止 mock-only；
- Gate失败时不得进入不可变发布；
- Gate成功后仍必须在Formal阶段验证真实发布证明链。

正式任务：

```text
V1_1_3_PRE_PUBLISH_TRUST_PATH_SHADOW_GATE_V1
```

### 平台基础 PF2-W3 — VM Session、三个 Profile Resource Lease 与长生命周期 Campaign Controller

正式目标模型：

```text
Acceptance Campaign
└── CandidateAcceptanceSession / FormalAcceptanceSession
    ├── FRESH_BASE ProfileResourceLease
    ├── DOCKER_BASE ProfileResourceLease
    └── RUNTIME_BASE_OFFLINE ProfileResourceLease
```

三个 Profile 不再共享：

```text
静态 IP
MAC
SSH alias
HostKeyAlias / known_hosts
反向代理端口
宿主转发端口
可写 staging root
proxy child process
Compose project identity
Provider mutable state
cleanup namespace
```

每个 Profile Resource Lease 必须独立绑定：

```text
session_id
profile_execution_id
profile
clone identity / VMX
base snapshot identity
MAC / control IP
SSH alias / HostKeyAlias / private known_hosts
host port lease
guest service endpoint
read-only candidate store reference
writable work / transfer / evidence root
proxy child lease or explicit NONE
Compose project / instance identity
lease state / expiry / heartbeat
```

Profile Lease 状态机：

```text
PLANNED
→ LEASED
→ CLONING
→ BOOTING
→ READY
→ EXECUTING
→ SEALED
→ CLEANING
→ RELEASED

failure
→ DIAGNOSTIC_RETAINED / QUARANTINED
```

源 VM 不可变检查改为 Session 级：

```text
Session start
→ original VM / snapshot full prestate hash

Profile lease
→ exact bound base identity check

concurrent execution
→ disposable clone count <= signed concurrency budget

Profile end
→ own clone shutdown + own lease release

Session end
→ original full poststate hash
→ running disposable clone count = 0
→ active lease count = 0
```

新增正式资源 Receipt：

```text
animemo.candidate-vm-session-resource-plan/v1
animemo.candidate-vm-profile-resource-lease/v1
animemo.candidate-vm-profile-resource-release/v1
animemo.candidate-vm-session-resource-reconciliation/v1
animemo.candidate-vm-source-immutability-session-receipt/v1
```

增加：

- Candidate / Formal Acceptance Campaign Controller；
- 失败 Clone 的有界 quarantine 与只读取证；
- Profile-local failure 的 selective retry；
- 正式重试默认创建 fresh clone，失败副本只用于诊断；
- Controller crash-resume 与真实 VMware、端口、代理、staging、Secret Lease和远端事务 reconciliation；
- Concurrency Scheduler 根据 CPU、内存、磁盘 IOPS、VMware与网络预算选择并发度1/2/3；
- 初期继续使用完整独立 Clone，linked clone必须经过独立安全合同；
- 保留原始 VM/Snapshot 不可变与Session级pre/post hash。

正式任务：

```text
V1_1_2_CANDIDATE_VM_SESSION_RESOURCE_COORDINATOR_AND_PROFILE_LEASES_V1
V1_1_2_PER_PROFILE_NETWORK_IDENTITY_SSH_KNOWN_HOSTS_AND_PORT_NAMESPACE_V1
V1_1_2_SESSION_LEVEL_SOURCE_VM_IMMUTABILITY_CONTRACT_V1
V1_1_2_ACCEPTANCE_CONTROLLER_CRASH_RESUME_AND_RESOURCE_RECONCILIATION_V1
V1_1_2_PARALLEL_CANDIDATE_AND_FORMAL_PROFILE_ORCHESTRATOR_V1
```

#### Online VM Profile 正式网络传输合同

新增：

```text
ONLINE_VM_PROFILE_NETWORK_TRANSPORT_CONTRACT_V1
animemo.proxy-transport-selection-receipt/v1
```

流量分层：

```text
BULK_IMMUTABLE_TRANSPORT
= APT package、固定工具链、immutable Release Asset、exact-digest blob

AUTHORITY_METADATA_READ
= GitHub API、provenance、Sigstore、R2 Origin等只读权威读取

LOCAL_CONTROL_TRAFFIC
= VMware、SSH/SCP到Guest、loopback、host-only、Docker socket、本地服务

OFFLINE_PROFILE
= 代理变量、代理端口、外网请求全部为0
```

规则：

- FRESH_BASE与DOCKER_BASE的安全在线大字节步骤默认优先使用本地代理；
- RUNTIME_BASE_OFFLINE永久禁止代理和外部网络；
- Proxy只是Transport，不是Release Source或Authority；
- canonical URL、host、subject、digest、certificate policy和source不得因代理变化；
- 只允许CONNECT/SOCKS纯转发，禁止TLS MITM与安装代理CA；
- Authority metadata禁缓存或强制freshness，每次操作只选一种传输路径，不自动fallback；
- bulk immutable下载只有在请求开始前proxy preflight失败时，才可在相同canonical origin和digest下选择direct；
- 请求状态不明后禁止盲目切换；
- Local control traffic必须进入NO_PROXY；
- Guest不得使用宿主`127.0.0.1`，由Provider preflight解析Guest可达host-only/NAT endpoint；
- Proxy状态由Session Proxy Broker持有，每个在线Profile获得独立child lease；
- 不设置全局ambient proxy环境；
- Candidate OCI、Installer Materials优先通过内容寻址本地staging传输，不为使用代理重新从公网拉取；
- DOCKER_BASE不得修改daemon.json、Docker systemd proxy drop-in或重启Docker daemon。

`ProxyTransportSelectionReceipt`至少记录：

```text
session_id
profile_execution_id
stage
canonical_source
selected_path = PROXY | DIRECT
proxy_alias
TLS_mode
cache_policy
bytes
duration
throughput
fallback_count
digest_result
request_state_closed
```

该Receipt并入`VM Profile Timing Receipt`，但两者保持独立Schema。

正式任务：

```text
V1_1_2_ONLINE_PROFILE_PROXY_EGRESS_CONTRACT_AND_TRANSPORT_SELECTION_V1
V1_1_2_SESSION_PROXY_BROKER_AND_GUEST_REACHABILITY_PREFLIGHT_V1
V1_1_2_PROXY_TLS_NO_MITM_CACHE_AND_NO_PROXY_SECURITY_GATE_V1
V1_1_2_PROXY_PERFORMANCE_BASELINE_AND_BOUNDED_DIRECT_FALLBACK_V1
```

目标：

```text
三套 VM 墙钟时间
≈
最慢 Profile
而不是三个 Profile 之和
```

### 平台基础 PF2-W4 — Session Secret Broker（正式统一合同）

正式产品/路线名称：

```text
Session Secret Broker
```

正式合同名称：

```text
Unified Session Secret Broker v1
animemo.session-secret-broker/v1
```

二者是同一个系统，不建立“sudo broker”“R2 broker”“proxy broker”等多套平行Secret缓存器。它负责整个Acceptance Campaign与无人值守Session中的统一Credential Lease，而不是各脚本反复要求输入sudo、R2、SSH或代理凭据。

管理：

```text
R2 Object Read-only
R2 Mirror Writer
Guest SSH / sudo
Shared Host SSH / sudo
GitHub Release Operator
Cloudflare Operator
Proxy Authentication
Backup Passphrase
Temporary Database Admin
Registry Read
```

原则：

- 同一 Campaign、同一 Provider、同一资源 Scope 的凭据人工输入一次；
- Read 与 Write Lease 分离，不自动提权；
- Secret 只存在于受控内存、OS Secret Store 或指定 Child Process；
- 不进入命令行、普通全局环境、Evidence、Receipt、Git、日志或项目长期 Memory；
- Controller 只记录 `lease_id / alias / scope / expiry / access event`；
- 隐藏输入失败时保留用户明确授权的聊天应急输入通道，但不得复述或持久化；
- Broker/Controller 重启时优先通过 OS Secret Store opaque reference 恢复；
- Agent 独立波次 Go Agent 接管前先冻结语言无关 Lease/RPC Contract。


新增正式Secret Receipt（不含值、Hash、长度、前后缀）：

```text
animemo.session-secret-lease-receipt/v1
animemo.secret-child-injection-receipt/v1
animemo.session-secret-reconciliation-receipt/v1
```

统一规则：

- 同一Campaign、同一Provider、同一exact scope、同一permission class的有效Credential只允许人工输入一次；
- Guest sudo、R2 read-only、R2 mirror writer、GitHub、Cloudflare、SSH、Proxy、Backup passphrase、DB临时管理员分别使用独立Lease；
- R2 read-only Lease不得自动升级为writer；
- child process通过匿名管道、Named Pipe、受控stdin、credential_process或本地IPC获取短期Child Lease；
- 禁止命令行、普通全局环境、Evidence、日志和项目长期Memory；
- 隐藏输入首选，聊天Secret只作为用户明确授权的应急通道；
- 有效Lease存在却再次询问时记录`SECRET_BROKER_REUSE_CONTRACT_DEFECT`；
- Campaign完成、硬截止、用户撤销、远端失效、权限变化或泄漏时立即revoke；
- Agent 独立波次由Go Agent接管Single Writer，但语言无关Lease/RPC Schema保持不变。

### 平台基础 PF3-W5 — Build-once、证据复用与 Artifact 去重

历史任务标识（保留原约定名称，实际落地窗口在完成计时基线后的 平台基础 PF3）：

```text
V1_1_2_BUILD_ONCE_OCI_AND_TRUSTED_TREE_EVIDENCE_REUSE_V1
```

该高风险优化只有在以下条件满足后启动：

```text
至少 20 轮真实计时
Reference Stable / RC Evidence 已封存
重复 Build 被证明为显著成本
Authority Invalidation Matrix 已冻结
```

实现：

- 唯一 Candidate Material Producer；
- API/Web OCI 只构建一次；
- Installer Materials、Candidate Input、Manifest Input、Build Receipt 一次生成；
- Performance、Candidate VM、Portable、Publish、Stable 只消费 exact bytes；
- 禁止消费者重新构建；
- Evidence 分类：`TREE_PURE / COMMIT_BOUND / ENVIRONMENT_BOUND`；
- 只复用 exact tree、workflow digest、toolchain identity、input digest 完全相同的 TREE_PURE 证据；
- Commit-bound 与 Environment-bound 继续重跑；
- 大字节 Candidate Artifact 单份存储，小型 Gate 只上传 Receipt；
- 内容寻址 wheelhouse、Node store、Go module cache必须绑定 lock digest，cache hit不能替代摘要验证；
- 公开 PR不得写 trusted-main cache。


### 平台基础 PF3-W6 — Release Portable 两层 Identity 与三份正式 Receipt

这里的 `Portable` 指发行链中的离线安装/验证 Portable Artifact，不是 v2.2 的用户 Portable Data Export；两者必须使用不同Schema和术语。

正式新增两层Identity：

#### Layer A — Canonical Portable Payload Identity（权威）

```text
animemo.portable-payload-identity/v1
```

绑定：

```text
repository
release tag
source commit / tree
canonical asset role
canonical release subject
canonical asset filename
payload SHA-256
payload byte size
manifest identity
checksums identity
provenance expected subject
schema version
```

它回答：

> **这份Portable内容在Release Authority中究竟是谁。**

#### Layer B — Local Portable Transport Instance Identity（非权威）

```text
animemo.portable-transport-instance/v1
```

绑定：

```text
payload identity digest
session_id
profile_execution_id
local staging filename
local source path identity
guest destination path identity
transport channel
transport byte digest
materialized_at / transferred_at
```

它回答：

> **同一份权威Payload在本次执行中被放在哪里、以什么本地名字传输。**

硬规则：

- `portable-payload.tar`等内部暂存basename永远不能进入`expectedSubjects`；
- canonical subject只从Release Manifest、Tag和Asset Role派生；
- local filename/path变化不得改变Payload Identity；
- Transport Identity不得授予Release Authority；
- 同一Payload可产生多个Transport Instance，但必须全部指向同一Payload digest；
- local bytes与Payload digest不一致时fail closed。

正式新增三份Receipt：

```text
animemo.portable-materialization-receipt/v1
animemo.portable-transfer-receipt/v1
animemo.portable-consumption-receipt/v1
```

语义：

1. `PortableMaterializationReceipt`
   - 证明canonical payload identity被物化为本地字节；
   - 记录local name/path为非权威观察值；
   - 验证SHA-256、size、member manifest和原子写入。
2. `PortableTransferReceipt`
   - 证明Host→Guest或staging→consumer传输前后字节一致；
   - 记录transport、source/destination identity与cleanup；
   - 不使用basename构造Release Subject。
3. `PortableConsumptionReceipt`
   - 证明Installer/Frozen Verifier消费了exact payload digest和canonical expected subject；
   - 证明local transport name被忽略；
   - 绑定验证结果、consumer版本和policy identity。

三份Receipt均为执行证据；Authority仍来自Canonical Portable Payload Identity、Manifest、Checksums与发布证明链。

正式任务：

```text
V1_1_3_RELEASE_PORTABLE_TWO_LAYER_IDENTITY_AND_THREE_RECEIPTS_V1
```

### 平台基础 PF3-W7 — Pre-Publish Trust Path Shadow Gate正式落地

W2阶段先冻结合同，本阶段将其接入Qualification和Publish前置依赖：

```text
Qualification
→ fixed provenance fixture shadow verification
→ Candidate Acceptance
→ Freshness
→ Pre-Publish Trust Path Shadow Gate readback
→ Immutable Publish
```

新增非权威Receipt：

```text
animemo.pre-publish-trust-path-shadow-receipt/v1
```

Receipt绑定：

```text
exact main/tree
workflow digest
producer/verifier identity
fixture identity
canonical subject identity
Sigstore policy identity
positive/negative matrix digest
result
```

任何Shadow失败都必须在不可变发布前停止；发布后Formal仍验证真实provenance，Shadow不得替代Formal。

### 工作流优化 Gate

```text
GATE_TIMING_RECEIPT_COMPLETE=YES
UNEXPLAINED_SKIPPED_GATE_COUNT=0
DETERMINISTIC_FAILURE_AFTER_EXPENSIVE_WORK_COUNT=0
CANDIDATE_PROFILE_RESOURCE_COLLISION_COUNT=0
FORMAL_PROFILE_RESOURCE_COLLISION_COUNT=0
SAME_SCOPE_SECRET_REPROMPT_COUNT=0
BUILD_ONCE_CONSUMER_REBUILD_COUNT=0
EVIDENCE_REUSE_WITHOUT_EXACT_TREE_BINDING_COUNT=0
WORKFLOW_DAG_CYCLE_COUNT=0
```

最终目标不是把 Full Stable 压到十分钟，而是：

> **普通反馈更快、一个候选尽可能一次发现全部问题、失败不再重复支付整条流水线成本，同时最终权威证明保持完整。**

---

## 平台基础 PF1 — Namespace、仓库结构与合同权威

在工作流 W1/W2 同期完成：

```text
AniMemoDevs Organization 迁移
Monorepo 顶层边界：apps / packages / infra / release
OpenAPI / JSON Schema / RPC Contract Registry
Data Authority Matrix
Container Data Ownership
Redis Requiredness Analysis
Third-party Adoption Policy / Dependency Tiers / ADR
Compression / Archive / Secret Envelope Engine Evaluation
```

顺序要求：

- Organization 迁移早于 Go module path、GHCR namespace、OIDC subject 与 Agent attestation最终冻结；
- 大目录移动使用独立 PR，只移动路径，不同时改行为、Schema 或运行逻辑；
- `apps/api` 正式命名为 `apps/core`，因为 Django承载领域模型、Migration、权限、Admin、Outbox与插件Host API；
- API与Worker初期可以共享同一后端OCI，只使用不同启动命令；
- 高权限 Host Agent 与低权限 Plugin Supervisor 永远不是同一进程。

### Data Authority Matrix

必须冻结：

```text
PostgreSQL
=
业务事实、用户 Memory、Durable Job、Outbox、任务终态、插件持久元数据、媒体引用

Redis
=
缓存、限流、短期通知、可重建协调状态

R2 / S3
=
对象字节

PostgreSQL MediaObject
=
对象身份、Key、Hash、Owner、引用与生命周期

Container Writable Layer
=
永远不得承载唯一数据
```

执行 Redis total-loss test：清空 Redis 后，业务事实、Job终态、用户Memory与授权关系必须无损。

---

## 平台基础 PF2 — Durable Runtime Foundation

必须在 v2.2 首次生产前完成，而不是等到 v2.5 的 AI 任务出现后才临时建立。

实现：

```text
PostgreSQL Jobs
Job Attempts
Transactional Outbox
Lease / Heartbeat
Retry
Idempotency
Cancellation
Scheduler
Python Worker
Job Status API
一个真实长任务迁移
```

权威关系：

```text
PostgreSQL = Job Authority
Python Worker = Executor
Redis = 可选唤醒和热状态，不是任务权威
```

至少迁移一个真实消费者，例如：

```text
Provider Sync
大型 Export
媒体处理
插件扫描
历史补发
```

同时冻结 Update Agent 的语言无关合同与 Golden Corpus：

```text
animemo.agent-capabilities/v1
animemo.update-plan/v1
animemo.operation-journal/v1
animemo.operation-receipt/v1
animemo.agent-rpc/v1
animemo.agent-handover-receipt/v1
```

平台基础 PF2 仍由 Python Host Agent 承担 active writer，不在这里提前双写。

---

## 平台基础 PF3 — Authority、Recovery 与基础可观测性

实现：

```text
Redis Total-loss Recovery
Container Recreate
Media Reservation / Verify / Finalize 基础
Backup Coverage Matrix
Restore-to-new
Structured JSON Logs
Stable Error Code Registry
Trace Context Propagation
Core Metrics
统一 Secret / PII / Path Redaction
Audit Event
```

统一 Context：

```text
instance_id
release_identity
request_id
trace_id
operation_id
job_id
plugin_call_id
update_operation_id
backup_id
restore_id
```

贯穿：

```text
Web → API → PostgreSQL → Outbox → Worker → Plugin RPC → Agent
```

本阶段不要求立即部署完整 Prometheus/Grafana/Loki/Tempo/Collector 集群；先闭合代码与Contract，并保证 Telemetry丢失不等于业务状态丢失。

---

## 平台基础 PF4 — 安全债、代码生命周期与平台合同清理

### Code Scanning 与异常边界

执行：

```text
V1_1_1_CODE_SCANNING_ALERT_INVENTORY_REACHABILITY_AND_OWNERSHIP_V1
V1_1_1_EXCEPTION_INFORMATION_EXPOSURE_AND_SECRET_SAFE_ERROR_BOUNDARY_REPAIR_V1
V1_1_1_PRODUCTION_SECURITY_ALERT_BURN_DOWN_V1
```

要求：

- 重新读取当前 default branch 全部告警；
- `UNTRIAGED_CODE_SCANNING_ALERT_COUNT=0`；
- 生产可达 Critical/High 清零；
- Medium 有明确 Owner、Repair或Evidence-based Dismissal；
- 对外只返回稳定error code、通用消息和correlation ID；
- 原始exception、traceback、stderr、SQL、绝对路径、SDK错误只能进入脱敏内部日志；
- 无证据 dismissal、过期 risk acceptance 均为0；
- v2.2 Beta 前生产可达 Medium或更高必须清零。

### Deprecated / Legacy / Dead Code、废弃功能与代码精简

建立完整生命周期：

```text
Legacy Surface Inventory
→ Production / Dynamic / Workflow / Schema / Operator Reachability
→ Replacement Contract
→ Data Migration Requirement
→ Deprecation Decision
→ Removal Wave
→ Removal Receipt
→ Post-removal Regression / Size / Complexity Review
```

覆盖对象不仅是代码，还包括：

```text
废弃功能
旧CLI入口
旧API alias
旧Schema reader/writer
旧Workflow
旧Installer/Updater路径
旧Feature Flag
重复Authority
无调用Helper
过期Fixture
不再支持的兼容层
重复文档与生成物
```

原则：

- 不在发行关键路径顺手清理；
- test-only、dead、generated与production callsite明确区分；
- 先证明Replacement是唯一Authority，再删除旧实现；
- 删除条件要求production、dynamic import、workflow、schema、operator和migration引用全部为0；
- 同一PR不同时做大路径移动、业务重构和大规模删除；
- 首次生产前可以clean break，但必须迁移真实长期数据；
- 删除旧路径后删除临时compat layer，不保留永久双轨；
- 已冻结外部Contract如需breaking，必须单独Contract Review；
- 代码精简以减少重复Authority、不可达分支和维护面为目标，不以压缩行数为目标；
- 每个Removal Wave必须可独立审查、回滚和复现；
- 删除后执行full text/symbol/workflow/schema/fixture sweep和自然CI。

正式任务：

```text
V1_1_4_LEGACY_SURFACE_INVENTORY_AND_REACHABILITY_V1
V1_1_4_DEPRECATED_FEATURE_AND_CODE_REMOVAL_WAVES_V1
V1_1_4_COMPATIBILITY_LAYER_RETIREMENT_AND_CODE_SIMPLIFICATION_V1
```

正式Gate：

```text
UNMAPPED_LEGACY_SURFACE_COUNT=0
REMOVAL_WITHOUT_REPLACEMENT_AUTHORITY_COUNT=0
REMOVAL_WITH_LIVE_PRODUCTION_REFERENCE_COUNT=0
PERMANENT_DUAL_PATH_COUNT=0
DUPLICATE_AUTHORITY_COUNT=0
DEPRECATED_FEATURE_WITHOUT_OWNER_OR_EXPIRY_COUNT=0
```

### 其他平台合同

- Staff高风险操作step-up 2FA；
- Transactional Email Outbox；
- Audit log schema/redaction/retention；
- Plugin Manifest单一Schema Authority；
- Plugin metadata read path不得激活Runtime；
- Plugin handler error ID、health与circuit breaker；
- DRF Browsable API仅开发环境；
- 统一产品版本元数据；
- requirements绝对路径清理；
- Metadata Provider Contract v1；
- Metadata Provider与Availability Provider分离；
- 用户override优先于Provider conflict policy；
- Version Manager Backend；
- Integration poll/write优化；
- Developer SDK与Contract Registry；
- Digest-bound SBOM与供应链组件清单，在v2.2生产前闭合。

---

## 平台基础 PF5-H — Homepage Identity & Privacy

定位：

> **让首页的数据身份与页面模式一致：匿名只看到明确公开的数据，登录用户立即看到自己的动漫记忆，任何身份切换都不发生跨用户残留。**

A07 #231 已完成公共目录、统计、筛选、详情与长字段有界读取。已合并的公共安全和身份修复不重开；PF5 仅承接仍未实现的个人首页及后续 UI 继承工作。当前公共读取以 [Public Catalog 合同](public-catalog-contract.md)、[API v1 合同](api-v1-contract.md) 和 `backend/journal/urls.py` 为准。

后续个人首页和平台 UI 的实施以前置部署参考 Stable 完成及独立授权为条件。保留 H1/H2 和 V1_* 稳定任务 ID，不把旧路由视为兼容承诺。

### H1 — Homepage Public Visibility Contract

正式任务：

```text
V1_1_5_HOMEPAGE_PUBLIC_VISIBILITY_CONTRACT_V1
```

合同：

- 原设计路径 `/api/v1/homepage/` 已退场并返回 410；当前公共 `AllowAny` 数据通过 `/api/v1/public/homepage/entries/`、`summary/`、`facets/` 等有界入口提供；
- 只使用显式有效的 `SiteSettings.homepage_owner`；
- owner 缺失、无效或禁用时返回安全空首页；
- 禁止回退到“第一个启用管理员”；
- 匿名结果只包含 `visibility=PUBLIC` 且未删除的记录；
- `PRIVATE / UNLISTED` 匿名暴露数必须为0；
- 公共统计、列表、筛选、详情按已实现的分离有界 DTO 提供，保持其授权范围和准确性；不恢复旧无界 `stats / results` 合并响应；
- 不做数据库迁移；未来如需精选非PUBLIC内容，使用独立curated collection合同。

### H2 — Authenticated Homepage Personalization

正式任务：

```text
V1_1_5_AUTHENTICATED_HOMEPAGE_PERSONALIZATION_V1
```

实现：

```text
拟议 GET /api/v1/homepage/me/（H2 未来个人入口，非当前已实现路由）
→ IsAuthenticated
→ owner = request.user
→ owner自己的PRIVATE / UNLISTED / PUBLIC记录
```

个人响应只提供首页需要的最小DTO：

```text
display_name
avatar_url
stats
results
```

禁止：

- 调用者传 `user_id / owner_id / username`；
- 返回无关数据库用户ID、email、public_slug或权限；
- GET隐式创建`UserSettings`；
- 个人空状态回退管理员数据；
- 个人错误回退公共、共享或Demo数据；
- 个人响应进入共享缓存。

缓存合同：

```text
Cache-Control: private, no-store, max-age=0
Pragma: no-cache
Vary: Authorization, Cookie
```

### H3 — Homepage Mode & Request Identity Gate

页面建立单一闭合模式：

```text
OWNER_PREVIEW
SHARED
DEMO
PERSONAL
SITE
```

路由/显式模式优先于登录态，已登录用户访问`/shared/:publicSlug`仍然显示共享对象。

每次读取必须绑定：

```text
homepage_request_key
request_generation
AbortController / Axios signal
```

要求：

- 身份变化时先清除旧用户记录；
- 旧请求晚到不得覆盖新身份；
- focus、records-updated、登录/退出和账号切换不得形成无序覆盖；
- React StrictMode不产生重复stale commit；
- Demo只能由显式Demo模式触发；
- loading、empty、error三态明确；
- `aria-busy`和可访问状态文本存在。

### H4 — v2.1 继承合同

v2.1 Homepage Memory Editorial视觉迁移可以重写布局和组件，但必须保留：

- SITE/PERSONAL/SHARED/DEMO身份边界；
- 双端点语义；
- 公共PUBLIC-only可见性；
- 个人no-store；
- request-key竞态防护；
- 空状态/错误不回退；
- 插件不得依赖页面内部Auth、Base UI或请求状态。

正式Gate：

```text
HOMEPAGE_ANONYMOUS_PRIVATE_UNLISTED_EXPOSURE_COUNT=0
HOMEPAGE_ARBITRARY_FIRST_STAFF_FALLBACK_COUNT=0
HOMEPAGE_PERSONAL_CALLER_OWNER_OVERRIDE_COUNT=0
HOMEPAGE_PERSONAL_SHARED_CACHE_COUNT=0
HOMEPAGE_CROSS_USER_STALE_RENDER_COUNT=0
HOMEPAGE_STALE_RESPONSE_COMMIT_COUNT=0
HOMEPAGE_PERSONAL_AUTOMATIC_FALLBACK_COUNT=0
HOMEPAGE_SHARED_ROUTE_OWNER_OVERRIDE_COUNT=0
HOMEPAGE_PUBLIC_RESPONSE_SHAPE_DRIFT_COUNT=0
```

详细合同：

```text
Homepage Identity & Authenticated Personal Entry Contract v2
```

---

## 平台基础 PF5～PF6 — Pre-v2.1 Consolidation Gate

在进入UI大改前执行：

```text
20轮门禁计时基线
Workflow优化收益复核
TREE_PURE复用安全复核
Durable Worker真实任务复核
Redis全丢复核
Agent Golden Corpus冻结
Security Alert治理复核
Legacy Removal复核
Data Authority复核
Backup/Restore覆盖复核
```

Gate：

```text
PLATFORM_FOUNDATION=PASS
UNTRIAGED_SECURITY_ALERT_COUNT=0
OPEN_PRODUCTION_REACHABLE_CRITICAL_HIGH_COUNT=0
JOB_AUTHORITY_OUTSIDE_POSTGRES_COUNT=0
UNRECOVERABLE_REDIS_STATE_COUNT=0
UNOWNED_PERSISTENT_DATA_PATH_COUNT=0
UNEXPLAINED_WORKFLOW_TIME_REGRESSION_COUNT=0
```

---

# 插件平台横向子路线（v2.1-P → v2.4-P）

这是一条嵌入现有 Master Roadmap 的横向子路线，不改变各 Minor 的核心使命：

```text
v2.1 Memory Experience / UI UX 2.0
v2.2 Anime Memory
v2.3 Safe Expansion
v2.4 Long-term Self-host
```

完整合同由《AniMemo 插件兼容与扩展边界设计文档》维护；Master Roadmap 只保留版本归属和 Gate。

## v2.1-P — Plugin Compatibility Baseline

在 UI/Query/Form 重构中保持 Plugin SDK v2 兼容；建立 Adapter、Design Token、声明式设置、官方插件回归、最低 Enable/Disable/Uninstall 语义，以及少量 `EXPERIMENTAL` Semantic Slots。

不实现完整不可信 Runtime，不允许未知第三方 Backend Plugin。

Gate：

> **Core 内部实现已经变化，但现有 SDK v2 官方插件无需修改，且没有泄漏 Base UI、TanStack Query、RHF、Zod 或 Lexical 内部 API。**

## v2.2-P — Trusted Memory Capability Surface

在 Anime、Character、Episode、Memory、Collection、Activity、Yearly Memory 等 Core Contract 稳定后，向官方和可信插件开放版本化、Actor-bound、Owner-scoped 的只读与细粒度写能力。

写能力必须区分：

```text
memory.suggest
memory.create
memory.update_own
memory.attach_media
collection.item.add
collection.item.remove
```

不得以宽泛 `memory.write` 绕过用户确认、Revision、Visibility、Receipt、Backup、Restore 或卸载语义。

Gate：

> **插件能够扩展 Anime Memory，但不能拥有、静默创建、删除、孤立或危及用户 Memory。**

## v2.3-P — Isolated Plugin Runtime v3

作为 Safe Expansion 的组成部分，建立：

```text
Versioned RPC
Plugin Supervisor
Worker / Container isolation
Capability Gateway
Filesystem boundary
Network egress policy
CPU / Memory quota
Timeout / Crash isolation
Secret mediation
Package identity
Untrusted UI boundary
Health / Quarantine / bounded diagnostics
```

Runtime Generation、Manifest Schema、SDK API、Capability Contract、RPC Protocol 和 Design Tokens 独立版本化；Runtime v3 不自动意味着 SDK v3。

Gate：

> **恶意或损坏的插件不能破坏 Core Memory、攻陷 AniMemo 实例或拖垮无关插件。**

## v2.4-P — Plugin Lifecycle & Operations

作为 Long-term Self-host 的组成部分，完成：

```text
Upgrade Plan
Permission delta review
Compatibility Matrix
Backup / Restore UI
Immutable package recovery
Plugin-owned state export
Uninstall Preview
Diagnostic history
Quarantine review
Migration / repair plan
```

最低 Health、Package verification、Quarantine 和 Capability revoke 必须已在 v2.3-P 随 Runtime 安全边界落地；v2.4-P 负责长期维护体验，而不是第一次补安全能力。

Gate：

> **即使插件包或发布者多年后消失，普通管理员仍能理解状态、恢复 Core Memory、导出插件状态，并在不依赖开发者 SSH 救援的情况下完成维护。**

## 插件子路线长期不变量

```text
Plugin Runtime v3 != Plugin SDK v3
Publisher identity != Runtime trust
Plugin-created Core Memory != Plugin-owned state
Core transaction commits before plugin event delivery
Event delivery may be at-least-once; mutation must be idempotent
Missing plugin package must not block Core Memory restore
```

实施前提：

```text
DURABLE_DEPLOYMENT_REFERENCE_STABLE=PASS
```

部署参考 Stable 发行收口和发行事务不得混入该子路线实现。


## 第一方官方扩展专项（v2.1-P-FP → v2.4-P-FP）

本专项是插件平台横向子路线的子合同，不建立第二套 Plugin SDK、Capability、Runtime、Event、Package 或 Backup 权威。

父级关系：

```text
Plugin Compatibility & Extension Boundary Contract v2
        ↓
First-party Official Extension Boundary Contract v1
```

### v2.1-P-FP0 — Policy / Identity / State

冻结：

```text
Core-or-Extension Decision Gate
ANIMEMO_FIRST_PARTY Publisher Identity
Distribution / Runtime / Activation独立分类
publisherId + pluginId + version + content/archive digest
installationId
Package / Install / Enable / UserGrant / Health状态分离
Core-bundled Baseline不可变
First-party ADR与Conformance Kit
```

正式任务：

```text
V1_2_P_FP0_FIRST_PARTY_EXTENSION_POLICY_IDENTITY_AND_STATE_CONTRACT_V1
```

### v2.1-P-FP1 — Reference Extension Audit

`watch-history-importer` 只作为目标Reference Extension，在完成以下审计前不得预先宣告PASS：

```text
exact manifest/package identity
forbidden Core imports / ORM bypass
no import-time side effect
Capability-only mutation
Actor / Owner / Installation binding
Data Policy
Core Memory与External Identity来源归属
Disable / Uninstall
Restore without package
SDK v2 regression
```

正式任务：

```text
V1_2_P_FP1_WATCH_HISTORY_IMPORTER_REFERENCE_GAP_AUDIT_AND_CONFORMANCE_V1
```

### v2.1-P-FP2 — Bundled Baseline Authority

建立：

```text
release/bundled-extensions.json
Core Release Manifest binding
closed package inventory
离线验证
disabled means no import/network/job/migration
no in-place bundled update
```

正式任务：

```text
V1_2_P_FP2_BUNDLED_EXTENSION_BASELINE_MANIFEST_AND_OFFLINE_AUTHORITY_V1
```

它与 `插件边界独立波次 Plugin Boundary Foundation` 对齐：第一方扩展先使用同一Serializable DTO、Capability Gateway、Post-commit Event和低权限Supervisor边界；未知第三方Backend Plugin仍保持禁用。

### v2.2-P-FP — First-party Memory Consumers

在对应Core Contract稳定后，第一方扩展使用：

```text
anime/character/episode read
memory.read / suggest / create / update_own
collection item add/remove
Core-owned External Identity / Import Receipt
Event / Idempotency
```

禁止宽泛 `memory.write`；Media、通用Job和网络能力继续服从父级前置条件。

正式任务：

```text
V1_3_P_FP_FIRST_PARTY_MEMORY_CAPABILITY_CONSUMERS_V1
```

### v2.3-P-FP — First-party Runtime v3 Migration

迁移顺序：

```text
Test-only Conformance Fixture
→ Read-only First-party Canary
→ watch-history-importer
→ Bangumi / Network Provider
→ Third-party enablement
```

正式任务：

```text
V1_4_P_FP_FIRST_PARTY_ISOLATED_RUNTIME_CANARY_AND_IMPORTER_MIGRATION_V1
```

### v2.4-P-FP — Official Package Operations

完成：

```text
Official Catalog Snapshot
Content-addressed Package Store
Bundled Baseline + immutable Override Resolver
Independent update plan
Permission delta review
Package bytes backup/recovery
Restore disabled pending review
Upgrade/Migration/Rollback
Advisory / Quarantine
```

正式任务：

```text
V1_5_P_FP_OFFICIAL_PACKAGE_STORE_CATALOG_AND_INDEPENDENT_UPDATE_OPERATIONS_V1
```

### 第一方专项长期不变量

```text
Official publisher != unrestricted runtime
Bundled baseline != mutable installed directory
Official catalog != package byte authority
slug + version != complete long-term identity
Provider mapping / import receipt != disposable plugin state
AniMemo-owned historical format reader != optional plugin
Restored plugin != automatically enabled
```

详细合同：

```text
AniMemo First-party Official Extension Boundary Contract v1
```


------

# UI 平台与 Memory Editorial 横向子路线（v2.1-U）

本子路线属于 `v2.1 Memory Experience / UI UX 2.0` 的正式组成部分，不改变 v2.1 的顶层使命，也不提前实现 v2.2 的 Memory 数据能力。

## v2.1-U 的正式技术权威

```text
React 19
+
Vite
+
Tailwind CSS 4
+
Base UI primitives
+
shadcn/ui local source
+
AniMemo components/ui
+
CSS Transitions / Animations
+
Motion for React
+
GSAP（仅特殊 cinematic surface）
```

权威关系必须固定为：

```text
Base UI
=
底层交互 Primitive Authority
焦点、键盘、ARIA、Portal、Overlay、Dismiss、交互状态

shadcn/ui
=
本地组件源码脚手架、组合参考与更新来源
不是第二套 Primitive Authority
不是业务代码直接依赖的黑盒运行时

AniMemo components/ui
=
业务代码唯一基础组件 API
稳定 Props、Variant、受控/非受控语义与错误边界

Tailwind CSS 4
=
样式编译、布局、响应式和 Token 映射层

AniMemo Design Tokens
=
颜色、排版、间距、圆角、阴影、Focus、Motion 与视觉语义权威
```

固定不变量：

```text
UI_PRIMITIVE_AUTHORITY_COUNT_PER_COMPONENT_KIND=1
SHADCN_IS_LOCAL_SOURCE_NOT_RUNTIME_AUTHORITY=YES
BUSINESS_CODE_DIRECT_BASE_UI_IMPORT_COUNT=0
PLUGIN_DIRECT_UI_ENGINE_IMPORT_COUNT=0
PLUGIN_DIRECT_TAILWIND_CLASS_CONTRACT_COUNT=0
```

## Import Boundary

正式依赖方向：

```text
Pages / Features
        ↓
Product Components
        ↓
Patterns
        ↓
components/ui
        ↓
Base UI
```

电影化视觉单独隔离：

```text
Universe / Hero / Cinematic Experience
        ↓
components/visual
        ↓
Motion / GSAP
```

约束：

- `@base-ui/*` 只允许在 `components/ui` 与其专用测试中导入；
- `motion/*` 只允许在 `components/ui`、`patterns`、`components/motion` 或明确批准的 Product Adapter 中导入；
- `gsap` 只允许在 `components/visual` 与 Cinematic Route 中导入；
- 页面、业务 Feature、插件和 Domain Hook 不得直接依赖底层 UI/动画引擎；
- 使用 ESLint `no-restricted-imports` 或等价机器门禁实施，而不是仅写在文档中；
- GSAP 必须 Route-level dynamic import，不得进入 Staff、Settings、Auth 等无关首屏 Chunk。

## 三层动画所有权

普通动画不再只分为 Motion 与 GSAP 两层，而是：

```text
第一层：CSS Transition / CSS Animation
第二层：Motion for React
第三层：GSAP
```

### CSS

负责：

- Hover、Focus、颜色、透明度；
- 简单 translate / scale；
- Skeleton；
- 静态 Reduced Motion fallback；
- 不需要 React 生命周期的轻量反馈。

### Motion for React

负责：

- Dialog / Sheet 内容进退场；
- `AnimatePresence`；
- 列表增删和重排；
- Layout Animation；
- 拖动排序；
- 手势反馈；
- 页面局部过渡；
- 普通 Achievement Unlock Overlay；
- 全局 `reducedMotion="user"` 或等价统一策略。

### GSAP

只负责：

- Universe；
- Hero；
- Poster 空间运动；
- 多段精确 Timeline；
- 复杂滚动叙事；
- 特殊年度记忆开场；
- 低频电影化视觉编排。

硬规则：

```text
同一 DOM 节点的同一 CSS 属性
只能有一个动画所有者
```

禁止：

```text
Motion 控制 transform
+
GSAP 同时控制同一节点 transform
```

GSAP 生命周期必须通过统一 Adapter、`gsap.context()` 或等价机制闭合，页面卸载后：

```text
ACTIVE_ORPHANED_GSAP_TIMELINE_COUNT=0
ACTIVE_ORPHANED_SCROLL_TRIGGER_COUNT=0
```

## 视觉语言

AniMemo 使用同一 Design System 下的三种 Presentation Mode，而不是三套互不兼容的 UI 系统。

### Memory Editorial

适用于：

```text
Homepage
Journal
Anime Detail
Character Memory
Memory Moment
Timeline
Yearly Memory
Collection
Memory Gallery
Long-running Episode Experience
```

关键词：

```text
柔和
克制
编辑式排版
私人手账与影像档案
相册式留白
时间感
图像优先
长期可阅读
```

不等于：

```text
标准 shadcn 黑白 Dashboard
满屏玻璃
高饱和霓虹
满屏纸张纹理
所有内容都放进圆角卡片
过度贴纸、胶带和手写字体
```

### Cinematic

适用于：

```text
Universe
Hero
特殊 Poster 展示
年度记忆开场
低频重大视觉时刻
```

关键词：

```text
深色
空间
景深
光影
Poster 主导色
精确编排
低频使用
```

### Calm Utility

适用于：

```text
Staff
Settings
Backup
Restore
Doctor
Plugin Management
Audit
Jobs
Release / Operation Status
```

关键词：

```text
清楚
可扫描
密度适中
低动画
危险操作突出
状态精确
```

三种模式共享：

- Typography Scale；
- Semantic Color；
- Focus；
- Form；
- Button；
- Dialog；
- Error；
- Permission；
- Responsive；
- Accessibility。

## Design Token Contract

正式冻结：

```text
animemo.design-tokens/v1
```

Token 分四层：

```text
Foundation Tokens
→ 原始颜色、字体、字号、间距、圆角、阴影、时长、缓动

Semantic Tokens
→ background、surface、text、accent、danger、warning、success、border、focus

Component Tokens
→ dialog、card、nav、sheet、poster、memory-thread

Motion Tokens
→ instant、fast、normal、slow、standard、emphasized、cinematic
```

插件只依赖稳定的 AniMemo CSS Variables，不依赖：

```text
bg-zinc-900
rounded-xl
text-sm
shadow-lg
Base UI internals
Motion internals
GSAP internals
```

## Poster 动态强调色

Poster-derived color 只能是内容级临时 Token，不得覆盖全站核心语义。

允许用于：

- 装饰线；
- 局部渐变；
- Hero 光晕；
- 小面积背景；
- 选中和进度强调。

禁止直接用于：

- 正文文字；
- Danger / Error；
- Focus ring；
- 唯一状态表达；
- 全页面背景。

必须经过：

```text
亮度约束
饱和度约束
前景对比度检测
Light/Dark fallback
High Contrast fallback
```

## Tailwind CSS 4 迁移边界

禁止在同一 Wave 同时完成：

```text
Tailwind 3 → 4
+
Base UI 全量接入
+
所有组件重写
+
全站视觉改版
```

必须拆分：

```text
v2.1-U0 — Current UI / Overlay / Animation / Browser Baseline
v2.1-U1 — Tailwind 4 Compatibility Migration
v2.1-U2 — Design Tokens v1
v2.1-U3 — Primitive Adapter & Local shadcn Source
v2.1-U4 — Patterns & Responsive Shell
v2.1-U5 — Page Migration & Memory Editorial
v2.1-U6 — Cinematic Boundary & Performance Closure
```

Tailwind 4升级阶段原则上保持视觉不变；只有构建、CSS入口、Dark Mode、Utility兼容、浏览器基线与Visual Regression闭合后，才进入视觉重构。

TypeScript继续渐进迁移：

```text
.js / .jsx
→
.ts / .tsx
```

UI Primitive迁移不要求等待全仓TypeScript完成；但新组件必须有稳定Props和未来类型化边界。

## shadcn 本地源码治理

禁止：

```text
shadcn add --all
在main直接运行生成器
未经审计覆盖本地组件
把shadcn默认主题当作AniMemo主题
```

每个组件必须：

```text
dry-run / view
→ diff
→ 许可证与上游版本记录
→ 单组件写入
→ 本地Adapter审计
→ 键盘/焦点/Reduced Motion测试
→ 独立提交
```

`components.json` 的 Primitive、Style、Base Color、CSS Variables策略必须先在隔离实验分支冻结，进入Active Roadmap后不得随意切换。

## 组件迁移顺序

不能用 `AnimeModal` 作为第一个 Base UI 迁移样本。

顺序：

```text
Button / Input / Field
→ Checkbox / Switch / Tooltip
→ AlertDialog / Toast
→ 简单 Settings Dialog
→ Mobile Sheet
→ Select / Combobox / Popover / Menu / Tabs
→ AnimeModal 外壳
→ Responsive Shell
→ Product Pages
```

迁移 `AnimeModal` 第一轮只替换：

```text
Dialog Root
Portal
Backdrop
Popup
Focus
Dismiss
Scroll Lock
Reduced Motion
```

不得同时重写数据请求、业务表单、内容结构和整体视觉。

## Accessibility Definition of Done

Base UI 不能替代AniMemo对最终可访问性的责任。

每个Primitive/Pattern至少验证：

```text
Tab / Shift+Tab
Escape
方向键
初始焦点
关闭后焦点恢复
Accessible Name
aria-describedby
错误信息关联
Focus Visible
颜色对比度
200% Zoom
Touch Target
Reduced Motion
Mobile Safe Area
iOS Scroll Lock Recovery
```

正式指标：

```text
UNLABELED_INTERACTIVE_CONTROL_COUNT=0
FOCUS_RETURN_FAILURE_COUNT=0
REDUCED_MOTION_CONTRACT_FAILURE_COUNT=0
CONTRAST_HARD_FAILURE_COUNT=0
TOUCH_TARGET_HARD_FAILURE_COUNT=0
```

## 正式任务

```text
V1_2_0_UI_BASELINE_BROWSER_COMPATIBILITY_AND_VISUAL_REGRESSION_V1
V1_2_0_TAILWIND4_COMPATIBILITY_AND_DESIGN_TOKEN_FOUNDATION_V1
V1_2_0_BASE_UI_SINGLE_PRIMITIVE_AUTHORITY_AND_SHADCN_LOCAL_SOURCE_V1
V1_2_0_MOTION_POLICY_GSAP_CINEMATIC_BOUNDARY_AND_REDUCED_MOTION_V1
V1_2_0_RESPONSIVE_SHELL_ACCESSIBILITY_AND_OVERLAY_HOST_V1
V1_2_0_MEMORY_EDITORIAL_PRESENTATION_SYSTEM_AND_PAGE_MIGRATION_V1
```

## v2.1-U Gate

```text
UI_PRIMITIVE_AUTHORITY_COUNT_PER_COMPONENT_KIND=1
DIRECT_BASE_UI_IMPORT_OUTSIDE_COMPONENTS_UI_COUNT=0
DIRECT_GSAP_IMPORT_OUTSIDE_COMPONENTS_VISUAL_COUNT=0
MOTION_GSAP_SAME_NODE_SAME_PROPERTY_CONFLICT_COUNT=0
SHADCN_UNREVIEWED_GENERATED_COMPONENT_COUNT=0
TAILWIND4_UNEXPLAINED_VISUAL_REGRESSION_COUNT=0
DESIGN_TOKEN_CONTRACT_VERSION=animemo.design-tokens/v1
PLUGIN_DIRECT_TAILWIND_CLASS_CONTRACT_COUNT=0
GSAP_UNRELATED_ROUTE_INITIAL_CHUNK_COUNT=0
ACCESSIBILITY_DOD=PASS
RESPONSIVE_DESKTOP_TABLET_MOBILE=PASS
REDUCED_MOTION=PASS
VISUAL_LANGUAGE_MEMORY_EDITORIAL=PASS
CALM_UTILITY_STAFF=PASS
CINEMATIC_BOUNDARY=PASS
```

Gate问题：

> **AniMemo 是否已经用一套可维护、可访问、可替换的底层交互系统，形成属于自己的 Memory Editorial 视觉，而不是变成套用组件主题的普通 Dashboard？**

实施前提：

```text
DURABLE_DEPLOYMENT_REFERENCE_STABLE=PASS
```

历史RC19的Qualification Finalizer与发行事务不得混入本子路线实现。

------

# v2.1 — Memory Experience / UI UX 2.0


## v2.1 版本分层（v9 继承）

v2.1 仍以 Memory Experience / UI UX 2.0 为顶层使命，但同时承载首次生产前的 Web Platform、Go Agent Shadow、Plugin Boundary 与产品 UX Contract Freeze。

### v2.1.0 — Web Platform、UI Platform 与 Memory Editorial

顺序：

```text
Current UI / Browser / Overlay / Animation Baseline
→ TypeScript 基础（渐进，不要求一次全仓转换）
→ OpenAPI Generated Types / Client
→ AniMemo API Transport
→ Query Key Factory
→ TanStack Query Adapter
→ React Hook Form / Zod Adapter
→ Tailwind 4 Compatibility Migration
→ AniMemo Design Tokens v1
→ Base UI 单一 Primitive Authority
→ shadcn/ui 本地源码与 components/ui Adapter
→ CSS / Motion / GSAP 所有权
→ Patterns / Responsive Shell
→ Memory Editorial 页面迁移
```

要求：

- React + TypeScript + Vite；
- UI基础组合为 `Tailwind CSS 4 + Base UI + shadcn local source + AniMemo components/ui`；
- 前端按 `app / features / entities / shared` 组织；
- OpenAPI由DRF Serializer/View/Schema继续作为权威；
- Generated Type不接管Cookie、CSRF、401刷新、Retry或业务Mutation；
- Query/Form/Editor通过AniMemo Adapter使用，不向Plugin SDK泄漏第三方内部API；
- Base UI负责交互Primitive，shadcn只作为本地源码生成与组合参考；
- `components/ui`是业务代码唯一基础组件API；
- 每类UI Primitive只有一个Authority；
- 动画采用 `CSS → Motion → GSAP` 三层所有权；
- Memory Editorial、Cinematic、Calm Utility共享同一Token/Primitive；
- Tailwind 4兼容迁移与全站视觉改版必须分Wave；
- TanStack Virtual用于千集Episode、大量Memory、Timeline与相册，后端仍使用分页/Cursor。

### install.animemo.cc

分阶段正式路线：

```text
v2.0.0 / 部署参考 Stable 后的平台基础波次 — Trust & Functionality Shell
安全、动态、无占位符的Bootstrap/Transport站点

v2.1.0 — Design System & Guided Install UX
跟随Design System完成视觉、移动端、无障碍和安装流程重构

v2.4 — Long-term Install / Upgrade / Doctor Portal
展示兼容矩阵、升级计划、诊断入口与长期运维帮助
```

部署参考 Stable 后的平台基础波次最低能力：

- 只展示canonical Stable/RC resolver提供的真实版本；
- 动态展示Tag、Asset、SHA-256、Attestation、Manifest与Transport来源；
- 复制安装命令时固定version和asset identity，禁止`latest`；
- GitHub与R2只作为明确选择的Transport，不自动source fallback；
- 安全Header、CSP、no-secret、no-private-coordinate、无占位符；
- 明确Loading、Empty、Blocked、Authority Unavailable状态；
- 安装脚本本身只做Bootstrap，仍由GitHub Immutable Release和frozen verifier授权；
- 不管理DNS、TLS、Reverse Proxy或Panel。

v2.1.0视觉与交互：

- 与AniMemo Design System共享Token/Primitive；
- Desktop/Tablet/Mobile；
- Keyboard、ARIA、Reduced Motion、Contrast；
- Stable/RC通道解释、安装前Compatibility Preview、命令分步说明；
- 不为了视觉重构改变Installer Trust Contract。

始终保持：

```text
install.animemo.cc = Bootstrap Transport
GitHub Immutable Release = Release Authority
```

站点不得使用`latest`；Mirror只改变获取Transport，不改变Authority。

正式任务：

```text
V1_1_X_INSTALL_PORTAL_TRUST_FUNCTIONALITY_AND_DYNAMIC_RELEASE_BINDING_V1
V1_2_0_INSTALL_PORTAL_DESIGN_SYSTEM_RESPONSIVE_ACCESSIBILITY_REBUILD_V1
V1_5_INSTALL_PORTAL_LONG_TERM_UPGRADE_DIAGNOSTICS_EXPERIENCE_V1
```

### Agent 独立波次 — Go Agent Shadow 与Single-writer切换

迁移顺序：

```text
Read-only Shadow
→ Plan Shadow
→ Material Verification / Staging
→ Low-risk Mutation
→ Apply / Rollback / Resume
→ Handover Receipt
→ active_writer=go
→ Python Host Agent退役
```

硬规则：

```text
active_writer=python
或
active_writer=go

永远禁止 active_writer=both
```

Go Agent负责高权限宿主控制面；低权限Plugin Supervisor保持独立进程和权限域。

### 插件边界独立波次 — Plugin Boundary Foundation

本阶段不是完整第三方市场，而是Runtime v3的前置边界：

```text
Serializable Plugin DTO
Closed RPC Schema
Capability Gateway
Secret Mediation
Post-commit Event
Idempotency
Unprivileged Supervisor
Process Isolation
Crash Isolation
Resource Limits
```

官方插件先通过与第三方相同的Capability Adapter，防止官方插件继续依赖Django ORM。

同时完成第一方专项的Reference Gap Audit与Bundled Baseline Authority：

```text
watch-history-importer不预先宣告Reference PASS
release/bundled-extensions.json绑定exact Core Release
Bundled baseline不允许原地独立覆盖
Package/Install/Enable/UserGrant/Health状态分离
```

第三方Backend Plugin在此Gate通过前保持禁用；完整第三方隔离、容器、Egress、Package Store、Official Catalog与Lifecycle仍在v2.3-P/v2.4-P。

### v2.1-C — 产品与UX合同冻结

在全站视觉重构前冻结以下设计，避免新UI完成后再次推倒：

```text
LONG_RUNNING_SERIES_DOMAIN_UX_AND_DATA_AUTHORITY_CONTRACT_V1
MEMORY_MOMENT_AND_YEARLY_MEMORY_DOMAIN_UX_CONTRACT_V1
ACHIEVEMENT_BADGE_UX_AND_DOMAIN_CONTRACT_V1
PRIVATE_MEDIA_DATA_AUTHORITY_MATRIX
```

只冻结：

- 路由、信息架构、低保真线框；
- DTO、Error、Permission、Event；
- 移动端、无障碍、减少动画；
- Long-running `CAUGHT_UP ≠ COMPLETED`；
- Memory Moment、展示柜、Unlock Overlay的Design System组件；
- Yearly Memory Revision与Item模型；
- Achievement/Badge不包含XP、用户等级、积分、排行榜、每日任务、赛季或任意插件规则。

不在旧UI重复实现完整功能。

---

核心问题：

> **用户是否愿意真的连续几年使用 AniMemo？**

因为 Memory 系统如果难用，就不会产生长期 Memory。

------

## 前端基础

```text
React 19 + Vite
Tailwind CSS 4
Base UI Primitive Authority
shadcn/ui Local Component Source
AniMemo components/ui
AniMemo Design Tokens v1
Responsive Shell
Accessibility
CSS / Motion / GSAP Motion Policy
Memory Editorial / Cinematic / Calm Utility
```

按照：

```text
Baseline
↓
Tailwind 4 Compatibility
↓
Tokens
↓
Primitives
↓
Components
↓
Patterns
↓
Responsive Shell
↓
Pages
```

推进，而不是全项目机械替换。

约束：

```text
shadcn != 第二套Primitive
Base UI != 业务直接依赖
Motion != 所有动画
GSAP != 全站默认动画运行时
Tailwind Utility != Plugin SDK
```

------

## Design System

统一：

```text
Foundation Tokens
Semantic Tokens
Component Tokens
Motion Tokens

Color
Typography
Spacing
Radius
Shadow
Elevation
Focus
Loading
Empty
Blocked
Error
Success
Poster-derived Content Accent
```

视觉呈现模式：

```text
Memory Editorial
Cinematic
Calm Utility
```

`Glass`只能作为受控视觉效果，不作为全站默认表面语言；Poster动态色只能生成内容级临时Token，必须经过对比度和Light/Dark/High Contrast回退。

------

## Responsive

真正支持：

```text
Desktop
Tablet
Mobile
```

Mobile：

```text
Bottom Navigation
Sheet
Touch-friendly controls
Mobile Dialog
Reduced Density
Gesture-friendly interaction
```

------

## Accessibility

正式进入 DoD：

```text
Keyboard
Focus
Screen Reader
Reduced Motion
Contrast
ARIA
Touch Target
```

------

## 页面重构

顺序：

```text
Shell
↓
Auth
↓
Homepage
↓
Journal
↓
Anime Detail
↓
Universe
↓
Staff
↓
Settings
```

Anime Detail 在这一阶段**提前预留 Character UI 结构**，但 Character Memory 数据能力主要在 v2.2。

------

## Universe

继续强化：

```text
悬停抽取
照片展开
透视
空间层次
景深
光影
```

同时增加：

```text
Mobile fallback
Reduced Motion
Keyboard
Performance budget
```

### v2.1 Gate

> 用户是否愿意长期在 Desktop 和 Mobile 上真正记录自己的动漫生活？

------

# v2.2 — Anime Memory


## v2.2.0 的生产定位（v9 继承）

```text
部署参考 Stable 后的平台基础波次 = PREPRODUCTION_ONLY
v2.1.x = PREPRODUCTION_ONLY
v2.2.0 = FIRST_FORMAL_PRODUCTION_VERSION
```

v2.2 不只是产品功能集合，也必须完成首次生产前的运行时与恢复验收。

### v2.2-alpha A — Local Identity、Time 与Long-running Series

在现有 Anime/Character/Time 基础上加入：

- Franchise使用关系表，不强制单父树；
- Episode AniMemo Local Identity；
- Provider Episode many-to-many Mapping；
- Provider Merge/Redirect与人工Split Repair；
- `EpisodeProgressRole = REQUIRED / OPTIONAL / EXCLUDED`；
- `UserWatchScope = MAINLINE / ALL_RELEASED / CUSTOM`；
- `ProgressAssertion = EXACT_EPISODE / APPROXIMATE_POSITION / CAUGHT_UP_AS_OF_PROVIDER_SNAPSHOT`；
- `EpisodeWatchEvent`不可变事实；
- `UserEpisodeState / UserAnimeState`为可重建投影；
- Provider Sync不得创建用户观看事实；
- 千集范围补录通过Durable `WatchRangeOperation`，冻结Episode集合、分批执行、可恢复、幂等；
- 千集UI使用Cursor、服务端过滤与虚拟列表。

### v2.2-alpha B — Private Memory Media 与Memory Moment

必须先具备最低安全媒体基础：

```text
MediaObject字节/Hash/类型/尺寸唯一权威
Private Media Delivery
Opaque Object Key
Reservation → External Write → Validate → Finalize
Durable MediaIngestJob
原图不可变
至少一种有界展示缩略图
基础单文件/像素/用户配额
临时对象过期
删除Receipt
Backup Object Inventory
```

然后实现：

```text
MemoryNote
MemoryAnchor
MemoryAttachment
MemoryMoment派生概念
Anime/Character/Episode关联
1～8张同一语义组截图
Spoiler / Visibility / Highlight
Memory Thread / Gallery / Search
```

`MemoryAttachment`只描述媒体如何参与Memory；字节、SHA、类型和尺寸只属于`MediaObject`。

### v2.2-alpha C — Achievement & Badge System v1

包含：

- Achievement Series/Tier；
- 受控Rule Registry；
- Badge Definition；
- Progress与不可变Unlock事实；
- 自动解锁和管理员授予/撤销；
- 历史补发预览、分批、暂停、恢复和幂等；
- 站内通知；
- Achievement Center；
- 最多6枚Badge Showcase；
- Unlock Overlay与`prefers-reduced-motion`；
- Audit与细粒度Permission。

明确排除：

```text
经验值
用户等级
积分
排行榜
每日任务
赛季
商城/交易
任意Python/SQL/表达式规则
第三方插件自定义执行规则
```

### v2.2-beta A — Yearly Memory Revision 与分享

正式模型：

```text
YearlyMemory
YearlyMemoryRevision
YearlyMemoryItem
```

FINALIZED Revision冻结：

- 选择项、顺序、标题、文案；
- 年度统计快照；
- 年度作品和角色；
- source cutoff；
- 用户时区；
- candidate algorithm version；
- display/visibility/spoiler snapshot。

删除私人媒体时删除全部可达字节，年度项目只保留文字快照和`SOURCE_MEDIA_DELETED`状态。

UNLISTED使用高熵Share Token、数据库仅存Hash、支持Expiry/Revoke/Rotate、默认`noindex`，不得提升源Memory长期可见性。

### v2.2-beta B — Production Readiness

必须完成：

```text
完整Upgrade演练
完整Backup/Restore-to-new
Redis Total-loss
Worker Crash/Lease Recovery
Agent断电恢复
Plugin Crash隔离
三套Candidate VM
三套Formal VM
Private Canary
Production Soak
```

安全Gate：

```text
UNTRIAGED_CODE_SCANNING_ALERT_COUNT=0
OPEN_PRODUCTION_REACHABLE_CRITICAL_HIGH_MEDIUM_COUNT=0
SECURITY_DISMISSAL_WITHOUT_EVIDENCE_COUNT=0
PRIVATE_MEDIA_URL_BYPASS_COUNT=0
FINALIZED_YEARLY_SILENT_MUTATION_COUNT=0
JOB_LOST_AFTER_WORKER_CRASH_COUNT=0
AGENT_DUAL_WRITER_COUNT=0
PLUGIN_CORE_PROCESS_ESCAPE_COUNT=0
```

---

这是整个产品最重要的产品里程碑。

目标：

> **AniMemo 到这里是否终于真的可以称作 Anime Memory？**

这一版以后执行 **Roadmap Freeze**，不继续无限增加概念。

------

# v2.2-A — Identity & Time

## Anime Local Identity

Anime 必须有独立 AniMemo identity。

External Provider：

```text
Bangumi
未来其他 Provider
```

只是 External Identity Mapping。

------

## Character Local Identity

Character 正式成为一等 Resource：

```text
Character
├─ AniMemo Local ID
├─ aliases
├─ external identities
└─ anime relations
```

绝不：

```text
character PK = provider ID
```

------

## Character Alias / Merge / Redirect

v2.2 支持：

```text
Canonical Identity
External Identity
Aliases
Merge
Redirect
```

Merge：

```text
Character B
↓
merged_into
↓
Character A
```

但 B 的历史身份仍然保留。

用户 Memory 不能 cascade 消失。

Split：

```text
NOT automatic lossless split
```

以后作为：

```text
manual repair workflow
```

处理。

------

## Time Semantics

必须拥有：

```text
occurred_at
recorded_at
time_precision
```

语义：

```text
occurred_at
= 事情什么时候真正发生
= nullable

recorded_at
= AniMemo 什么时候记录
= precise

time_precision
= EXACT
  DAY
  MONTH
  YEAR
  APPROXIMATE
  UNKNOWN
```

禁止因为只知道：

```text
2027 年
```

就伪造成：

```text
2027-01-01 00:00:00
```

并当成精确事实。

------

# v2.2-B — Memory Foundation

## Minimal Activity Primitive

例如：

```text
id
owner
activity_type
resource_identity
occurred_at
recorded_at
time_precision
metadata_version
visibility
```

但：

```text
× Kafka
× Generic Event Bus
× Heavy Event Architecture
```

路线审查此前也建议 Timeline 先建立 Minimal Activity Primitive，而不是直接进入大型 Event Architecture。

------

## Memory Anchor

定义：

```text
Memory Anchor
=
context / locator

NOT
first-class Memory Resource
```

第一版：

```text
episode
timestamp
quote
scene_label
freeform_context
```

例如：

```text
Anime: XXXX
Character: A

Anchor:
Episode 8
00:17:32
“……”

Memo:
“就是从这里开始喜欢这个角色。”
```

Anchor 不拥有：

```text
独立 Timeline
Visibility
Collection
External Identity
Activity stream
```

------

## Revision Semantics

正式采用：

```text
Current Projection
+
Meaningful Historical Revisions
```

不是完整 Event Sourcing。

### 必须保留历史

```text
Score
Watch Status
Rewatch
Favorite Character
```

### Memo

保留合理的 Revision History。

### 不永久记录无意义操作噪声

例如：

```text
UI drag/drop position
临时排序
展示设置
```

判断标准：

> **几年以后，这个变化是否帮助用户理解当时的自己？**

------

## Memory Thread

正式定义：

```text
Memory Thread
=
derived temporal projection

NOT
source of truth
```

即：

```text
Facts
↓
Projection
↓
Your Memory with <Anime>
```

来源：

```text
Watch History
Score History
Status History
Rewatch
Memo
Character Memory
Activity
Collections
```

第一版不建立：

```text
memory_thread source-of-truth table
```

以后可以缓存或 materialize，但必须可重建。

------

# v2.2-C — Anime Memory

正式能力：

```text
Watch History
Timeline
Score History
Status History
Rewatch History
Anime Memo
```

例如：

```text
2027
首次观看
Score 9.5

2030
重看
Score 8.2

2034
再次重看
Score 9.0
```

AniMemo 保存的是关系变化，而不是永远只保存最新：

```text
score = 9.0
```

------

# v2.2-D — Character Memory

正式加入：

```text
Character Resource
Anime ↔ Character Relation
Favorite Character
Character Memo
Character Activity
Character Timeline
Character Memory Thread
```

重点不是：

> 这个角色身高、血型、完整百科是什么？

而是：

> **这个角色与你之间发生过什么？**

例如：

```text
Character: XXX

2028-04-12
第一次标记喜欢

2028-04-12
Memo：
“第 8 集之后突然很喜欢 TA。”

2030-01-05
重看作品

2030-01-06
更新 Character Memo
```

第一版不要做：

```text
角色评分 1~10
Waifu 指数
角色排行榜
复杂 Affinity
```

------

# v2.2-E — Memory Organization

## Collections

继续：

```text
2028 暑假
大学时期
京阿尼
治愈系
年度最喜欢
人生神作
```

第一版主要保持：

```text
Anime Collections
+
Character Favorites
```

Mixed Resource Collection 可以以后再评估。

------

## Tags

服务：

```text
Anime
Memo
Character Memory
Memory organization
```

但不要把 Tag 演变成复杂 ontology。

------

# v2.2-F — Rediscovery

## Unified Search

搜索：

```text
Anime
Character
Memo
Collection
Tag
Year
Activity
Status
```

优先：

```text
PostgreSQL
+
FTS / Trigram
```

------

## Yearly Memory

加入：

```text
看过多少 Anime
完成多少
重看多少
评分变化
Memo 最多的作品
Favorite Character
第一次喜欢的角色
记录最多的角色
年度 Collection
年度关键词
```

例如：

```text
My Anime Year 2029

你完成了 37 部动画

这一年你第一次喜欢了 18 个角色

你记录最多的角色：
XXXX

你第一次标记喜欢 TA：
2029-03-17

后来留下了 7 条相关记忆
```

------

## History / Memory Thread

以后 Anime Detail 可以拥有：

```text
Your Memory with <Anime>
```

把：

```text
Watch
Score
Character
Memo
Rewatch
Collection
Activity
```

按时间组织。

------

# v2.2-G — Ownership

## Visibility Primitive

统一：

```text
PRIVATE
UNLISTED
PUBLIC
```

覆盖：

```text
Profile
Memo
Character Memory
Collection
Activity
Yearly Memory
```

不要每个功能自己造：

```text
memo_is_public
collection_shared
character_public
```

上传路线分析已经指出，统一 Visibility 能避免长期权限技术债。

------

## Portable Data Format

正式建立：

```text
animemo-export/v1
```

明确：

```text
Backup
=
Instance Recovery

Migration
=
Instance Relocation

Export
=
User Data Ownership
```

Portable Data 必须保存：

```text
stable exported identity
resource type
relationships
time semantics
visibility
merge/redirect semantics
schema version
```

------

## Portable Data Referential Integrity

必须保证：

```text
Anime A
 ↕
Character B
 ↕
Memo C
 ↕
Activity D
 ↕
Collection E
```

Export → Import 后仍然保持这些关系。

不是：

> 导出前 100 个对象，导入后也有 100 个对象，所以 PASS。

上传路线也已经明确建议 Portable Export Schema 应独立版本化。

------

# 全局 Memory Integrity Contract

从 v2.2 开始正式成为 Quality 横轴。

建议冻结：

```text
MI-1
External metadata disappearance MUST NOT delete user memory.

MI-2
Provider identity changes MUST NOT silently orphan user memory.

MI-3
Merge operations MUST preserve historical references.

MI-4
Upgrade / Import / Restore MUST NOT silently discard unsupported memory.

MI-5
Destructive ambiguity MUST fail closed or require explicit repair.
```

例如：

```text
Bangumi Character disappeared
↓
metadata unavailable
```

而不是：

```text
DELETE Character
↓
CASCADE Memo
↓
CASCADE Favorite
↓
CASCADE Activity
```

------

# v2.2 Anime Memory Gate

正式冻结成 8 问：

```text
① 我经历过什么？
   Timeline / Activity

② 它实际上什么时候发生？
   occurred_at / recorded_at / time_precision

③ 是什么让我记住它？
   Character / Memory Anchor

④ 当时的我是怎么想的？
   Memo

⑤ 后来我的看法怎么变化？
   Score / Status / Rewatch / Revision

⑥ 它属于我人生中的哪个阶段？
   Collections / Tags / Time

⑦ 多年以后我还能重新发现这段记忆吗？
   Search / Yearly Memory / Memory Thread

⑧ 即使 AniMemo 本身不存在了，
   我的记忆还属于我吗？
   Portable Data / Ownership / Memory Integrity
```

这 8 项都成熟以后：

> **才正式宣告 AniMemo 已从 Anime Tracker / Journal 跨入 Anime Memory。**

------

# v2.3 — Safe Expansion


## v2.3 范围澄清（v9 继承）

v2.3解决“规模化后仍然安全高效”，而不是首次补齐v2.2必需的安全基础。

扩展：

```text
多尺寸响应式媒体变体
WebP / AVIF Benchmark
高级配额与用量统计
高级GC与Orphan Reconciliation
同Owner内容去重
大规模R2成本优化
大规模Provider Incremental Sync
Episode Merge/Split管理工具
高风险插件容器隔离
完整Network Egress/SSRF Policy
Package Store / Publisher Identity
可选第三方Worker Runtime评估
```

只有现有PostgreSQL Job Contract和Python Worker运行数据证明需要时，才评估Dramatiq/RQ/Celery等执行器；不得产生第二任务权威。

---

这一版回答：

> **媒体越来越多、插件越来越多以后，用户积累多年的 Memory 还能安全吗？**

------

## Poster / Media Pipeline

统一：

```text
Anime Poster
Character Image
Memo Image
```

逐步进入：

```text
Fetch
↓
Validate
↓
Decode
↓
Resize
↓
Strip metadata
↓
Encode
↓
Hash
↓
Dedupe
↓
Reservation
↓
External Write
↓
Finalize
```

WebP 参数先 Benchmark，不冻结 `q85 / 1000×1500` 为永久协议。

继续保持：

```text
reservation
→ external write
→ finalize
```

以及：

```text
unknown remote orphan
→ never auto-delete
```

------

# Plugin Runtime v3

正式解决 trusted in-process Plugin 的长期问题。

```text
AniMemo Core
     │
     │ RPC
     ▼
Plugin Worker / Container
```

至少：

```text
Capability
Filesystem isolation
Network egress policy
CPU quota
Memory quota
Execution timeout
Crash isolation
RPC versioning
Plugin data lifecycle
Secret mediation
```

上传路线审查也建议 Runtime v3 补齐资源配额、网络和 RPC 生命周期能力。

------

## Secret Mediation

优先：

```text
Plugin requests operation
↓
Core uses Secret
↓
Plugin receives bounded result
```

而不是：

```text
Plugin asks Secret
↓
Core returns Secret
```

------

## UI Extension

Semantic Slots：

```text
anime.detail.actions
anime.detail.sidebar
journal.toolbar
dashboard.widget
settings.section
```

Core 继续控制：

```text
Security
Accessibility
Responsive
Critical rendering
```

------

## Theme Plugin

允许：

```text
tokens
theme assets
limited presentation slots
```

不允许替换：

```text
Auth
Admin Security
Updater
Critical Confirmation
```

------

# v2.3 Gate

> **一个插件失控、一个 Provider 消失或者媒体处理失败时，用户已经积累的 Memory 是否仍然安全？**

------

# v2.4 — Long-term Self-host


## v2.4 范围澄清（v9 继承）

v2.4在既有Long-term Self-host基础上收口：

- 完整OpenTelemetry Collector/Exporter、Dashboard与告警；
- API、Worker、Plugin、Agent、Backup/Restore跨进程Trace；
- Plugin Lifecycle & Operations完整UI；
- Package缺失恢复、`ORPHANED_PRESERVED`、恢复后默认禁用；
- Plugin Upgrade Plan、Schema Migration、Rollback与Compatibility Matrix；
- 基于首生产前 Go Agent 单写接管，完善 Unified Session Secret Broker 的长期运维与恢复；
- Doctor/Diagnostics历史、容量规划和自动修复建议；
- install.animemo.cc长期安装、升级、诊断和兼容体验；
- 多实例自托管运维能力，但不默认引入Kubernetes。

---

这是和 v2.2 对应的另一个最高级 Gate。

问题：

> **一个普通管理员安装 AniMemo 后，真的能省心维护很多年了吗？**

------

## Update UX

逐步实现：

```bash
animemo update
```

系统自动理解：

```text
CURRENT
Target
Release Identity
Compatibility
Migration
Backup Requirement
exact OCI digest
Health
Rollback capability
```

------

## Verified Release Evidence

原：

```text
ReleaseAuthoritySnapshot
```

改为：

```text
VerifiedReleaseEvidence
```

或：

```text
ReleaseAuthorityReceipt
```

必须绑定：

```text
repository
release
tag
source SHA
manifest SHA
API digest
Web digest
workflow identity
attestation identity
verification contract version
verified_at
```

Evidence 只是：

```text
verified cache
```

不能成为 Authority 本身。

------

## GitHub / GHCR Reliability

Authority 验证之后：

```text
exact pull transient fail
↓
retry exact pull
```

而不是：

```text
重新 Release Discovery
重新 workflow discovery
重新 attestation
```

只读 / 幂等 transient：

```text
max 5 bounded retries
```

但：

```text
publication
migration
apply
```

不进入 generic retry。

------

## Distribution Source

当前继续：

```text
GitHub Release
+
GHCR
```

只先明确：

```text
Distribution Transport Boundary
```

真实存在第二来源以后再抽：

```text
ReleaseSource interface
```

不要为了未来假想需求提前设计错误抽象。

------

## Backup / Restore UI

后台：

```text
Create Backup
Verify
Backup History
Compatibility
Migration Export
Restore Plan
```

Restore 必须：

```text
preflight
compatibility check
explicit confirmation
```

------

## Doctor Complete

最终：

```bash
animemo doctor
```

检查：

```text
OS
Architecture
Docker
Compose

PostgreSQL
Redis
API
Web

Updater
CURRENT
PREVIOUS
Release Identity

Backup
Restore Compatibility

Disk
Memory

Listen Endpoint
Public Origin
Proxy Headers

GitHub
GHCR
R2

Plugin Runtime
Permissions
```

------

## Diagnostics / System Settings

后台逐步管理：

```text
Site
Email
Bangumi
Turnstile
Media
R2
Plugins
Backup
Update
Health
Security
Audit
Capacity
```

目标：

> 普通维护不再需要频繁手工修改 `.env`。

------

# v2.4 Long-term Self-host Gate

假设用户：

```text
2027 安装
```

到了：

```text
2031
```

仍然能：

```text
更新
备份
验证备份
恢复
迁移
换域名
换反代
换面板
诊断
理解兼容状态
```

并且大多数情况不需要开发者 SSH 手工救。

这时候才可以正式回答：

> **是，AniMemo 已经适合长期自托管。**

------

# v2.5+ — Memory Intelligence / Rediscover & Understand

这里开始进入**高级能力**。

前提：

```text
Anime Memory Gate PASS
```

否则不做。

统一上层名称：

# Memory Intelligence

目标不是：

> 让 AI 替用户创造记忆。

而是：

> **帮助用户理解、连接、重新发现自己已经保存的 Memory。**

------

## AI Yearly Summary

基于：

```text
Timeline
Yearly Memory
Memo
Score History
Rewatch
Character Memory
Collections
```

生成：

```text
Structured facts
↓
Selected Memory evidence
↓
AI narrative
```

例如：

> 2029 年你完成了 37 部作品。你重新看了《XXX》，评分从两年前的 9.5 变成了 8.2。你这一年记录最多的角色是 XXX……

最重要的原则：

```text
AI ≠ source of truth
```

AI 只能：

```text
summarize
connect
explain
```

不能偷偷创造：

```text
watch history
score
favorite
memo
```

最好以后 AI 总结可以追溯到真实 Memory Evidence。

------

# Memory Graph

不是纯 Anime 百科关系图。

而是：

```text
                     User
                      │
          ┌───────────┼───────────┐
          │           │           │
       Anime       Character     Memo
          │           │           │
          └───────────┼───────────┘
                      │
                  Activity
                      │
                     Time
                      │
                Personal Memory
```

可以展示：

```text
Anime ↔ Character
Anime ↔ Collection
Character ↔ Memo
Memo ↔ Moment
Anime ↔ Rewatch
Memory ↔ Time
```

关系图只是已有事实的 Projection。

不提前建设大型 Knowledge Graph。

------

# Recommendation

AniMemo 的推荐不应该只是：

```text
看过 A
→ 其他人也看 B
```

以后可以有：

### Discover

```text
推荐还没看过、可能真正适合你的作品
```

### Rediscover

```text
推荐多年没想起来的旧作品
```

### Rewatch

```text
推荐现在可能值得重看的作品
```

### Character-based

```text
根据长期 Character Memory
发现相关作品
```

### Memory Recommendation

例如：

```text
五年前的今天你开始看《XXX》
```

或者：

```text
你已经四年没打开这个 Collection
```

这会比普通“猜你喜欢”更有 AniMemo 自己的特色。

------

# Memory Search Assistant

以后甚至可以询问：

```text
“我大学期间最喜欢哪些角色？”

“我什么时候开始喜欢京都动画？”

“有哪些作品第一次给高分，
后来重看反而降分了？”

“找一部我几年前写过 Memo，
但已经很久没想起来的动画。”

“2029 年我最常记录什么类型的角色？”
```

AI 使用：

```text
AniMemo structured Memory
```

回答。

而不是自己凭模型记忆猜。

------

# Memory Engine

进一步理解：

```text
第一次观看
重看
评分变化
角色变化
Memo 变化
Collection
人生阶段
长期偏好
```

例如：

```text
2027
喜欢 Character A

2030
重看后写：
“现在反而更能理解 Character B。”

2034
Character A 仍然存在于
“人生最喜欢角色”中。
```

------

# History Today

例如：

```text
五年前的今天：
你开始看《XXX》

三年前的今天：
你第一次喜欢 Character A

两年前的今天：
你写下这条 Memo
```

非常符合 AniMemo 的产品定位。

------

# Search Evolution

仍然：

```text
PostgreSQL
↓
FTS / Trigram
↓
真实瓶颈
↓
Dedicated Search
```

再决定：

```text
Meilisearch
OpenSearch
Elasticsearch
```

------

# PWA / Mobile

路线：

```text
Responsive Web
↓
Installable PWA
↓
Offline shell
↓
Read-only cache
↓
Notifications
↓
冲突模型成熟以后才做 Offline Mutation
↓
Capacitor
↓
确实需要才 Native
```

第一版 PWA 不做 Offline CRUD。

------

# Background Jobs（v9 继承）

这一节的旧表述“只有未来大量同步、媒体或AI需求出现才建立Worker”已经过期。

现有性能证据已经证明同步长任务会占满请求线程；同时Long-running Range、Media Ingest、Achievement Backfill、Provider Sync、Yearly Draft、Export、Backup与Plugin Jobs都需要Durable Runtime。

因此正式路线是：

```text
平台基础 PF2
PostgreSQL Durable Jobs / Outbox
+ Python Worker
+ 一个真实任务迁移

v2.2
Memory / Media / Achievement / Range 正式消费

v2.3+
按运行数据决定是否替换执行器
```

仍然不默认引入：

```text
Kafka
RabbitMQ
Celery作为第二权威
大型工作流引擎
```

------

# 未来重大 Contract Breaking 的独立审议政策

本次 v2.0.0 承接部署参考 Stable；未来若破坏已发布公共合同，应重新选择 major。以下是需要独立审议的破坏类型（其中 API、Resource、Deployment 等有各自版本）：

```text
API v2
Resource Identity v2
Deployment Contract v2
Auth major change
Plugin SDK breaking
重大 DB 模型变化
Multi-instance
Multi-tenant
Core Worker Architecture
Major Event Architecture
```

以下功能本身不构成 major 的充分条件；未来版本按相对已发布公共合同的真实破坏性选择，不预定下一个 major：

```text
新 UI
新动画
Character Memory
Yearly Memory
AI Summary
PWA
Recommendation
```

------



# 已接受的配套设计合同索引（v9 修订）

Master Roadmap只维护版本归属、依赖和Gate；详细设计由独立文档维护：

```text
Release Workflow & Time Cost Optimization
→ 门禁计时、风险分类、Performance分档、VM并行、Build-once、Evidence reuse

VM Acceptance Session / Resource / Transport Contracts
→ VM Profile Timing Receipt、Session + 三个Profile Resource Lease、在线Profile网络传输、Proxy Selection、Campaign Recovery

Release Portable Identity & Receipt Contract
→ Canonical Payload Identity、Local Transport Instance Identity、Materialization/Transfer/Consumption三份Receipt

Pre-Publish Trust Path Shadow Gate
→ 固定Actions provenance fixture、production loader、canonical Sigstore policy、real sigstore-go、frozen verifier

Unified Session Secret Broker
→ 多Provider Credential Lease、Child Injection、Reconciliation、隐藏输入与聊天应急通道

Preproduction Target Architecture
→ Django Core、Python Worker、Go Agent、Plugin Supervisor、Data Authority

Plugin Compatibility & Extension Boundary v2
→ v2.1-P～v2.4-P横向插件路线

Mature Open-source Capability Reuse
→ Adapter/Wrapper、ADR、依赖分级、压缩归档、age、OTel、UI引擎

UI Platform & Memory Editorial Design System v1
→ Tailwind 4、Base UI单一Primitive、shadcn本地源码、components/ui、CSS/Motion/GSAP、Presentation Modes、Import Boundary、Accessibility DoD

Homepage Identity & Authenticated Personal Entry Contract v2
→ 公共首页PUBLIC-only可见性、显式owner、安全空状态、个人/me端点、最小DTO、no-store、模式优先级与跨用户竞态闭合

Long-running / Ongoing Series Contract v1
→ 千集Episode、本地Identity、Caught-up、Progress Assertion、Range Job

Memory Moment & Yearly Memory Contract
→ 私人截图、MediaObject、Memory Moment、Yearly Revision、分享与删除

Achievement & Badge System v1
→ 分级成就、隐藏成就、Badge、Backfill、通知、展示柜

Security Alert Governance
→ Code Scanning Inventory、Error Boundary、Burn-down、Production Zero Gate
```

文档优先级：

```text
用户最新明确决策
>
已冻结Normative Contract
>
Master Roadmap v12
>
配套Design Draft
>
历史路线图/旧任务说明
```

------

# Roadmap Freeze Rule

从现在开始正式冻结主路线。

```text
ROADMAP FREEZE
```

未纳入已接受主路线的新能力默认：

```text
DEFER
```

只有满足以下至少一项才进入 Active Roadmap：

```text
1. closes a demonstrated Memory gap;

2. closes a demonstrated Durability gap;

3. prevents likely irreversible structural rework;

4. resolves measured production/user evidence;

5. is required for security,
   privacy,
   data integrity,
   or memory integrity.
```

下面这些理由不够：

```text
“看起来很酷”
“以后可能有用”
“竞品有”
“AI 能做”
“顺便加一下”
```

否则：

```text
Future Product Backlog
```

------

# Future Product Backlog

已经明确可以做，但现在不进入主线：

```text
AI Yearly Narrative
Memory Graph
Recommendation
Rediscovery Recommendation
Memory Search Assistant

Voice Actor Memory
Staff Memory
Character Relationship Graph
Episode Wiki
Quote Database
Scene Database
Advanced Character Affinity
Social Feed
Follow
DM
Knowledge Graph
Native App
Advanced Offline Sync
```

前五项属于：

```text
v2.5+ Memory Intelligence Candidates
```

后面的继续等待真实需求。

------

# 每个 Minor 的 Definition of Done

以后所有 minor 都必须至少检查：

```text
Functional             PASS
Data Integrity          PASS
Memory Integrity        PASS

Fresh Install           PASS
Upgrade                 PASS

Migration               PASS / N/A
Backup                  PASS
Restore                 PASS / N/A

Security                PASS
Privacy                 PASS

Accessibility           PASS
Responsive              PASS
UI Primitive Authority  PASS / N/A
Design Token Contract   PASS / N/A
Motion Ownership        PASS / N/A
Visual Regression       PASS / N/A
Memory Editorial        PASS / N/A
Homepage Identity       PASS / N/A
Homepage Privacy        PASS / N/A
Cross-user Race Safety  PASS / N/A

Performance             PASS / bounded
Compatibility           PASS

Docs                    PASS
Release Authority       PASS
Workflow Timing         PASS / bounded
VM Profile Timing       PASS / N/A
Profile Resource Lease  PASS / N/A
Online Transport Policy PASS / N/A
Risk Classification     PASS
Portable Identity Split PASS / N/A
Portable Receipts       PASS / N/A
Trust Path Shadow Gate  PASS / N/A
Job / Outbox Recovery   PASS / N/A
Redis Total-loss        PASS / N/A
Observability Context   PASS
Security Alert Triage   PASS
Session Secret Broker   PASS / N/A
Secret Lifecycle        PASS / N/A
Code Lifecycle          PASS / N/A
Install Portal Contract PASS / N/A
SBOM / Supply Chain     PASS / N/A
Roadmap Full Markdown   PASS
```

路线审查此前也建议给每个 Minor 建立统一 DoD。

------

# 最终版本地图

| 版本 | 定位 | 核心使命 | 生产状态 |
| --- | --- | --- | --- |
| **v1.0 / v1.0.x** | Historical Foundation | Release、Upgrade、数据安全基线 | 已有历史生产线 |
| **v2.0.0** | Durable Deployment Reference Stable | Installer、Backup、Restore、Migration、Release Trust | `PREPRODUCTION_ONLY`；可验证部署参考 Stable |
| **部署参考 Stable 后的平台基础波次** | Preproduction Platform Foundation | 工作流提速、VM Timing/Lease/Transport、Session Secret Broker、Portable两层Identity/Receipts、Trust Shadow、Durable Worker、数据权威、安全债、代码清理、Homepage Identity & Privacy | `PREPRODUCTION_ONLY` |
| **计划 v2.1.0** | Web Platform / UIUX 2.0 | TypeScript、OpenAPI Client、Tailwind 4、Base UI单一Primitive、shadcn本地源码、Design Tokens、CSS/Motion/GSAP、Memory Editorial、Responsive、UX Contract Freeze | `PREPRODUCTION_ONLY` |
| **Agent 独立波次** | Go Agent Handover | Shadow、Plan、Apply/Rollback/Resume、Single Writer | `PREPRODUCTION_ONLY` |
| **插件边界独立波次** | Plugin Boundary & First-party Extension Foundation | DTO、RPC、Capability、低权限Supervisor、Reference Extension审计、Bundled Baseline Authority | `PREPRODUCTION_ONLY` |
| **计划 v2.2-alpha** | Anime Memory Build | Identity、Time、Long-running、Memory Moment、Achievement、Yearly Revision | 生产候选构建期 |
| **计划 v2.2-beta** | Production Readiness | Worker/Agent/Plugin/Backup/VM/Canary/Soak完整演练 | 生产前封闭测试 |
| **计划 v2.2.0** | First Formal Production | 首次推荐承载真实长期用户数据 | `FIRST_FORMAL_PRODUCTION_VERSION` |
| **计划 v2.3** | Safe Expansion | 媒体规模化、完整Plugin Runtime v3、Provider规模化 | 正式扩展线 |
| **计划 v2.4** | Long-term Self-host | 完整运维、Plugin Operations、OTel、长期兼容 | 长期维护线 |
| **计划 v2.5+** | Memory Intelligence | Rediscovery、AI、Graph、Recommendation | 证据驱动扩展 |
| **未来重大破坏性变更（版本待定）** | 独立 SemVer 审议 | 相对已发布公共合同选择 major | 不自动提升生产资格 |

------


# 路线覆盖自检（v12）

本版本必须明确覆盖以下已接受事项：

| 已接受事项 | v12位置 | 状态 |
| --- | --- | --- |
| Session Secret Broker / Unified Session Secret Broker | 平台基础 PF2-W4、Agent 独立波次、v2.4 | INCLUDED |
| Release Portable两层Identity | 平台基础 PF3-W6 | INCLUDED |
| Portable Materialization / Transfer / Consumption三份Receipt | 平台基础 PF3-W6 | INCLUDED |
| Pre-Publish Trust Path Shadow Gate | 平台基础 PF1-W2合同、平台基础 PF3-W7落地 | INCLUDED |
| Deprecated Code / Feature删除与代码精简 | 平台基础 PF4 | INCLUDED |
| install.animemo.cc | 部署参考 Stable 后的平台基础波次、v2.1.0、v2.4 | INCLUDED |
| VM Profile Timing Receipt | 平台基础 PF1-W1 | INCLUDED |
| Online VM Profile正式网络传输合同 | 平台基础 PF2-W3 | INCLUDED |
| Session + 三个Profile Resource Lease | 平台基础 PF2-W3 | INCLUDED |
| 工作流与时间成本优化 | 平台基础 W 横向主线 | INCLUDED |
| 每次路线更新生成完整Markdown | 路线图产物治理 | INCLUDED |
| Base UI单一Primitive + shadcn本地源码 | v2.1-U、v2.1.0 | INCLUDED |
| Tailwind 4兼容迁移与视觉重构分Wave | v2.1-U1～U5 | INCLUDED |
| CSS → Motion → GSAP动画分层 | v2.1-U动画所有权 | INCLUDED |
| Memory Editorial / Cinematic / Calm Utility | v2.1-U视觉语言 | INCLUDED |
| AniMemo Design Tokens v1与Poster动态色安全 | v2.1-U Design Token Contract | INCLUDED |
| UI Import Boundary与插件隔离 | v2.1-U、v2.1-P | INCLUDED |
| Accessibility DoD与Reduced Motion | v2.1-U、v2.1 Gate | INCLUDED |
| shadcn生成源码审计治理 | v2.1-U shadcn本地源码治理 | INCLUDED |
| 公共首页显式Owner与PUBLIC-only可见性 | 平台基础 PF5-H H1 | INCLUDED |
| 登录态个人首页与最小DTO | 平台基础 PF5-H H2 | INCLUDED |
| Homepage Mode与跨用户请求竞态闭合 | 平台基础 PF5-H H3 | INCLUDED |
| v2.1首页视觉迁移继承数据身份合同 | 平台基础 PF5-H H4、v2.1页面迁移 | INCLUDED |
| First-party Official Extensions / Core Boundary | v2.1-P-FP～v2.4-P-FP | INCLUDED |
| Publisher / Distribution / Runtime / Activation四轴分类 | v2.1-P-FP0 | INCLUDED |
| Package Identity与Installation Identity分离 | v2.1-P-FP0 | INCLUDED |
| watch-history-importer Reference Gap Audit | v2.1-P-FP1 | INCLUDED |
| Core-bundled Baseline不可变与离线Authority | v2.1-P-FP2、插件边界独立波次 | INCLUDED |
| First-party细粒度Memory Capability Consumer | v2.2-P-FP | INCLUDED |
| First-party Runtime v3迁移Canary | v2.3-P-FP | INCLUDED |
| Official Catalog / Package Store / Independent Update | v2.4-P-FP | INCLUDED |

固定结果：

```text
ROADMAP_ACCEPTED_ITEM_INVENTORY_COMPLETE=YES
ROADMAP_PREVIOUS_ACCEPTED_ITEM_LOSS_COUNT=0
ROADMAP_COMPLETE_MARKDOWN_ARTIFACT_GENERATED=YES
```

# 三个最重要的长期 Gate

## Gate 1 — v2.2 Anime Memory

问：

> **AniMemo 能否真正保存“这部动画在我的人生里留下了什么”？**

包括：

```text
Anime
Character
Moment
Memo
History
Change
Time
Life Stage
Rediscovery
Ownership
```

------

## Gate 2 — v2.4 Long-term Self-host

问：

> **一个普通管理员能否在几年后仍然轻松更新、恢复、迁移和诊断自己的 AniMemo？**

------

## Gate 3 — v2.5+ Memory Intelligence

问：

> **AniMemo 能不能在不篡改事实的前提下，帮助用户重新理解和发现自己多年积累的记忆？**

AI、关系图、推荐算法都在这里接受这个 Gate。

------

最终可以把整个项目压缩成：

> **Preserve anime memories. Preserve the system that holds them. Help people rediscover what those memories mean over time.**

也就是：

```text
保存记忆
   ↓
保护记忆
   ↓
重新发现记忆
   ↓
理解记忆
```

我认为这版已经可以作为 **AniMemo 的长期 Master Roadmap**。从这里开始，与其继续增加主路线概念，更应该把新点子放入 Future Backlog，然后严格按 **v1.0 → v2.0 → v2.1 → v2.2** 一个阶段一个阶段落实。

---

## 本次路线更新产物声明

```text
ROADMAP_VERSION=v12
ROADMAP_UPDATE_COMPLETE_MARKDOWN_REQUIRED=YES
ROADMAP_COMPLETE_MARKDOWN_ARTIFACT_GENERATED=YES
ROADMAP_PARTIAL_ONLY_OUTPUT=NO
ROADMAP_PREVIOUS_ACCEPTED_ITEM_LOSS_COUNT=0
ROADMAP_UI_PLATFORM_TRACK_INCLUDED=YES
ROADMAP_MEMORY_EDITORIAL_INCLUDED=YES
ROADMAP_UI_PRIMITIVE_AUTHORITY_COUNT_PER_KIND=1
ROADMAP_HISTORICAL_RC_IDENTITIES_PRESERVED=YES
ROADMAP_HOMEPAGE_IDENTITY_PRIVACY_INCLUDED=YES
ROADMAP_FIRST_PARTY_OFFICIAL_EXTENSION_INCLUDED=YES
ROADMAP_FIRST_PARTY_EXTENSION_PARENT_CONTRACT_BOUND=YES
ROADMAP_BUNDLED_BASELINE_IMMUTABLE_REQUIRED=YES
ROADMAP_REFERENCE_PLUGIN_PASS_ASSUMED=NO
ROADMAP_HOMEPAGE_PUBLIC_VISIBILITY_PREREQUISITE=YES
ROADMAP_HOMEPAGE_PERSONALIZATION_AFTER_DURABLE_DEPLOYMENT_REFERENCE_STABLE=YES
```

# 路线状态（v12）

```text
ROADMAP_VERSION=v12
ROADMAP_FIRST_PARTY_OFFICIAL_EXTENSION_INCLUDED=YES
ROADMAP_FIRST_PARTY_EXTENSION_MODE=PLUGIN_PLATFORM_CHILD_TRACK
ROADMAP_FIRST_PARTY_EXTENSION_PARENT_CONTRACT_BOUND=YES
ROADMAP_BUNDLED_BASELINE_IMMUTABLE_REQUIRED=YES
ROADMAP_REFERENCE_PLUGIN_PASS_ASSUMED=NO
ROADMAP_FIRST_PARTY_IMPLEMENTATION_START=AFTER_DURABLE_DEPLOYMENT_REFERENCE_STABLE_AND_EVIDENCE_SEAL
ROADMAP_HISTORICAL_RC_IDENTITIES_PRESERVED=YES
ROADMAP_PREVIOUS_ACCEPTED_ITEM_LOSS_COUNT=0
```


## v12 变更覆盖核对

- 继承 Active v10 全部章节：Memory Axis、Durability Axis、Quality、Memory Integrity、Portable Data、第一方扩展、插件与 UI 子路线、安装入口、生产准备、长期自托管与未来需求。
- 部署参考 / Web / Memory / Safe Expansion / Long-term / Intelligence 六个里程碑及其横向回指已映射；平台、Agent、插件边界按波次组织。
- 原 v2.0 Breaking Platform 远期占位已改为独立 SemVer 决策政策，未擅自指定下一个 major。
- A03/A07/R01/CodeQL/gRPC 已合并；旧证据维持原提交和执行等级，正式 Qualification 必须在最终 exact main 重新取得。
- H1 的 `/api/v1/homepage/` 是已退场路由；H2 的 `/api/v1/homepage/me/` 是未来个人入口设计记录。A07 #231 已让六条旧公开读取路由返回 410；实际执行以当前 [Public Catalog 合同](public-catalog-contract.md)及当前 API 合同和源码为准，不恢复旧路由。此处保留 `/api/v1` 协议身份。
- HOST_AGENT 单写接管仍是 Web 后、首生产前门禁；Long-term Self-host 的 Go Agent 章节是在此前接管基础上完善长期运维，不能将首生产所需接管延后。
- UI/安装站点/平台工作均未因本次改号获得执行或上线授权。
