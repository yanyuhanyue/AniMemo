# AniMemo Standard Filesystem Layout v1

Status: FROZEN FOR v1.1

Version: v1

Scope: 冻结 AniMemo 标准安装、持久数据、Updater 程序、Updater 持久状态与本地运行时的目录边界，以及这些目录的 ownership、权限、生命周期、备份、恢复、迁移、删除和升级语义。

Non-goals: 本 Contract 不实现 Installer、Backup、Restore、Migration Bundle、Migration Secret Envelope 或 Doctor，不迁移任何现有实例，不定义 Docker、DNS、TLS、防火墙或公网反向代理的管理方式。

Compatibility: v1.1 新安装使用本文件的标准路径；现有 v1.0 实例的 `/opt/1panel/docker/compose/animemo/app` 是只读识别的 compatibility profile，必须通过未来显式 cutover 转换，不能原地改写 v1.0 Updater 常量、用 symlink 冒充标准路径或再次移动已经位于 `/data/animemo` 的数据。

Change policy: v1 冻结后，兼容性澄清和更严格的安全约束可以追加；任何改变路径语义、持久性、备份集合、删除边界或 discovery identity 的变更都必须有记录、兼容计划和新的 Contract 版本，不得静默修改。

相关 Contract：

- [Deployment Boundary v1](deployment-boundary-v1.md)
- [Installer Contract v1](installer-contract-v1.md)
- [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md)

## 1. 核心原则

AniMemo 的标准 filesystem roots 是：

```text
/opt/animemo
/data/animemo
/opt/animemo-updater
/var/lib/animemo-updater
/run/animemo-updater
```

这些路径是默认值，不是网络协议或 Release Authority。Installer Contract 明确允许 custom app/data root 时，custom root 必须满足本文件相同的安全和生命周期语义，并通过受验证的 instance locator 让 Updater 与 Doctor 发现；不能依靠进程当前目录、面板目录、环境猜测或 symlink。

必须保持以下生命周期分离：

- application/deployment material 位于 app root，不包含 secret；
- instance data 与受保护配置位于 data root；
- Updater 可执行程序与 Updater durable state 分离；
- runtime socket 只位于 `/run`，不能进入持久备份；
- cache、lock 和 credential 即使位于同一 state root，也必须使用独立子目录及不同的 backup/migration 规则。

目录存在本身不授权覆盖或删除。发现 foreign file、symlink/junction、路径逃逸、owner/mode 不符合 Contract、metadata 与 systemd allowlist 不一致或多个实例声称同一路径时，相关操作必须 fail closed。

## 2. Root contract

| PATH | OWNER + MODE | PERSISTENCE | BACKUP? | MIGRATE? | RESTORE? | CAN RECREATE? | SECURITY SENSITIVITY | DELETE SAFETY | UPGRADE OWNERSHIP |
|---|---|---|---|---|---|---|---|---|---|
| `/opt/animemo` | `root:root`, root `0755`; immutable/non-secret material 使用目录 `0755`、文件 `0644/0755`，禁止 group/world write | Installed deployment material | NO；不通过 instance backup 复制 Release material | NO；目标机从正式 Release Authority 重建 | 通过 exact immutable release identity 重新取得，不从数据 backup 覆盖 | YES | 高完整性、非机密 | 只能删除已验证为 AniMemo-owned、非 active、非 rollback-required 的具体 release；目录存在或同名不足以授权删除 | AniMemo Installer 写入；Updater 只按冻结接口读取；应用容器不得写入 |
| `/data/animemo` | root `root:root 0755`；子目录按第 3 节 | Durable instance data and protected configuration | YES，按子目录分类 | YES，按子目录分类 | YES，按子目录分类 | NO（整体） | 最高机密性与完整性 | Installer 永不自动清空；任何 destructive reset 不属于普通 install/update，必须是未来独立、显式、精确确认的流程 | Installer 创建边界；应用、数据库及未来 Backup/Restore/Migration 工具各自只管理被授权子目录 |
| `/opt/animemo-updater` | `root:root 0755`; release 内容不可 group/world write | Installed Updater program | NO | NO；目标机安装已验证 Updater artifact | 重新安装 exact verified Updater artifact | YES | 高完整性、通常非机密 | 只可回收非 `current`、非 rollback target 且由安装记录证明 ownership 的版本目录；foreign content 必须停止 | root 调用的 AniMemo Updater installer；运行中的 Updater 不自改程序根 |
| `/var/lib/animemo-updater` | `animemo-updater:animemo-api 0700`; durable files 默认 `0600` | Durable Updater state，内部含可重建 cache、credential 和 ephemeral lock 的明确例外 | SELECTIVE；见第 4 节 | SELECTIVE；见第 4 节 | SELECTIVE；先验证 schema、release identity 与目标 roots | PARTIAL | 高完整性；credential 子树为最高机密性 | 不得删除整根；只能按子目录规则清理 cache、expired plan 或已证明无引用的 state | Updater 独占写；Installer 仅原子创建/升级 schema 与 instance locator；API 只通过 Unix RPC 访问 |
| `/run/animemo-updater` | `animemo-updater:animemo-api 0750`; socket `0660` | Ephemeral；重启后可重建 | NO | NO | NO | YES | 高完整性的本地控制面；请求/响应可能含操作 metadata | 只允许删除已验证为 stale Unix socket 的路径；同名普通文件、link 或未知对象必须停止 | systemd `RuntimeDirectory` 与 Updater socket server |

`OWNER + MODE` 是最低基线。平台若使用等价的稳定 numeric UID/GID，instance metadata 和安装记录必须保存可验证映射；不得根据当前容器随机推断 ownership。

## 3. `/data/animemo` 子目录

标准 data root 至少包含以下生命周期区域：

| SUBPATH | OWNER + MODE | PERSISTENCE | BACKUP / MIGRATE / RESTORE | CAN RECREATE? | SECURITY SENSITIVITY | DELETE SAFETY |
|---|---|---|---|---|---|---|
| `config/` | `animemo-updater:animemo-api 0700`; secret-bearing files `0600` | Durable protected application configuration | YES；Backup/Restore 必须保留；Migration 必须等待 Secret Envelope | NO | 最高机密性与完整性 | 不得随 release replacement 删除；只允许原子更新及失败回滚 |
| `postgres/` | PostgreSQL service identity；最终 owner/mode 由受支持镜像与 Installer 明确建立，不能假定 `root:root` | Authoritative durable database storage | 不直接归档该目录；Backup/Migrate/Restore 使用第 6 节 logical database artifact | NO | 最高机密性与完整性 | 运行中和未知状态下绝不删除；普通 install/update 永不清空 |
| `redis/` | Redis service identity；最终 owner/mode 由受支持镜像与 Installer 明确建立 | Operational persistent cache / security throttle state | 默认不进入 instance disaster-recovery payload；Restore 后可重新创建 | YES，代价是 cache 与限流状态重置 | 中等机密性，高运行完整性 | 仅在 Redis 已停止且精确识别此实例时由显式维护流程重建 |
| `plugins/` | application UID/GID，当前兼容映射为 `10001:10001`; `0755`，写入文件不得 world-write | Durable installed/plugin-managed instance state | YES / YES / YES | NO（用户安装内容） | 高完整性，内容可能机密 | 不得因插件停用、release 切换或未知 package 自动删除 |
| `media/` | application UID/GID，当前兼容映射为 `10001:10001`; `0755` | Durable local media | Local backend: YES / YES / YES；remote object bytes 见第 7 节 | NO（本地 authoritative bytes） | 可能含私人媒体，高完整性 | 未建立稳定 MediaObject ownership 时不得删除；未知 remote orphan 永不自动删除 |
| `private/` | application UID/GID，当前兼容映射为 `10001:10001`; `0700`; files 默认 `0600` | Durable-or-lifecycle-bound private application state | 由未来 Backup Contract 逐项列入；active secret material 的 Migration 依赖 Secret Envelope | 逐项决定 | 最高机密性 | 不得把未识别文件当 cache；一次性 state 只能由其拥有的状态机消费/删除 |
| `backups/` | application UID `10001`, group `animemo-api`, `0770`; backup artifacts `0600` | Durable recovery artifacts | 不递归包含自身；是否随服务器迁移携带由 Migration Contract 明确 | 可重新生成新的 backup，但既有 recovery point 不可重建 | 最高机密性与完整性 | 仅按未来显式 retention policy 删除；未知、未校验或唯一可用 backup 不得删除 |
| `logs/` | application UID/GID，当前兼容映射为 `10001:10001`; `0755`，log files 不得 world-write | Operational / retention-bound | 默认 NO / NO / NO | YES | 可能含敏感 operational metadata | 只按明确 retention/rotation policy 清理，不跟随 release tree 删除 |

受保护配置不得再放入 `/opt/animemo` 的 release/deployment tree。v1.1 的 exact config filename/schema 可由配置 Contract 在不改变本节 lifecycle 的前提下细化；任何 secret-bearing config 都必须位于 `config/` 或更严格的受保护子目录，并由原子更新流程维护。

`config/`、`private/`、`backups/` 以及它们的父路径必须拒绝 symlink/junction；敏感文件必须拒绝 hard link，原子替换必须在同一受保护目录内完成并同步文件与目录 metadata。

## 4. `/var/lib/animemo-updater` 子目录与 instance locator

| SUBPATH | CLASS | BACKUP / MIGRATE / RESTORE | RECREATE | SECURITY / DELETE RULE |
|---|---|---|---|---|
| `instance.json` | Canonical non-secret instance locator | YES / YES / YES；必须先验证 schema 和 roots | 只能由 Installer 根据已验证实例原子重建 | `0600`, owner `animemo-updater`; 禁止 secret；不一致时 fail closed |
| `releases/` | CURRENT/PREVIOUS 与 immutable release history | YES / YES / YES | 可从 Release Authority 重建 artifact，但不能猜测实例当前状态 | 高完整性；active/current/previous generation 不得删除 |
| `operations/`, `plans/`, `runtime.json`, `runtime-images.env` | Durable operation/runtime state | YES；迁移与恢复按 schema/version 验证 | 部分可重建，但不得伪造或丢弃 PENDING/history 语义 | `0700` directories、`0600` files；只允许原子写入 |
| `bootstrap/` | One-time verified bootstrap input/state | 在 bootstrap 完成前保留；完成后的 retention 由 Updater Contract 决定 | exact release 可重新取得，但 bootstrap identity 不得猜测 | 高完整性；不得重复导入 CURRENT |
| `cache/` | Re-creatable release/network cache | NO / NO / NO | YES | 可以 scoped 清理；cache 永远不是 Release Authority |
| `gh/`, `.docker/` 及其他 credential store | Host credential state | 普通 Backup/Migration 禁止明文携带；迁移依赖第 7 节 Secret Envelope 或在目标机人工重新配置 | 可人工重新配置 | 最高机密性；不得进入 API、RPC、日志、Manifest 或 `instance.json` |
| `update.lock` 及同类 lock | Ephemeral concurrency state | NO / NO / NO | YES | 只能在证明无 active owner 后由其状态机处理，禁止 generic timeout takeover |

### 4.1 `instance.json` interface

固定路径：

```text
/var/lib/animemo-updater/instance.json
```

它是 Installer、Updater、Backup/Restore/Migration 与 Doctor 的 non-secret filesystem discovery interface。写入必须：

- 使用 versioned schema；
- 在同一目录创建 `0600` 私有临时文件，完成校验与 fsync 后 atomic rename；
- owner 为 `animemo-updater`，不得 group/world-readable；
- 拒绝 symlink、junction、hard link、非 regular file、路径逃逸和未知 schema；
- 保存 canonical absolute path；不得保存未经解析的相对路径或依赖 symlink 的 identity；
- 更新失败时保留上一份完整有效 locator，不留下半写状态。

精确 JSON schema 和实现推迟到 Installer implementation，但 v1 接口必须能表达：

- `schemaVersion`；
- `appRoot`；
- `dataRoot`；
- `deploymentProfile`，至少可区分 v1.1 standard 与 v1.0 compatibility profile；
- canonical `listen` identity；
- canonical `publicOrigin`；
- immutable `releaseIdentity`，足以绑定 version/channel/commit、Manifest 与 exact OCI digests。

`instance.json` 绝不能包含数据库密码、Django secret、credential encryption key、OAuth secret、provider token、GitHub/GHCR credential、migration passphrase 或任何可用于认证/解密的值。`listen` 与 `publicOrigin` 只用于 discovery；其语义由 [Public Origin / Listen Contract v1](public-origin-listen-contract-v1.md) 冻结。

### 4.2 systemd path allowlist

Updater 的 systemd read/write allowlist 必须由 Installer 从已验证、canonicalized 的 instance metadata 协调生成，且只授予：

- app root 的只读访问；
- data root 中明确需要的受限写访问；
- `/var/lib/animemo-updater` 的持久状态访问；
- `/run/animemo-updater` 的 runtime socket 访问。

metadata、systemd drop-in、实际 canonical path 或 deployment profile 任一不一致时，Updater 必须 fail closed，不能扩大为父目录、`/opt`、`/data` 或整个 filesystem。custom root 不得只更新环境文件而保留旧 allowlist。

## 5. v1.0 compatibility 与 future explicit cutover

当前 v1.0 profile 使用：

```text
application: /opt/1panel/docker/compose/animemo/app
data:        /data/animemo
```

这是 compatibility evidence，不是 v1.1 新安装布局。Phase 1 不移动生产文件，也不修改 frozen v1.0 Updater 的 fixed-path fail-closed behavior。

未来 cutover 必须是显式、可观测、可回滚的 AniMemo-scoped operation：

1. 验证 source instance、当前 immutable release identity、data root 和 protected config。
2. 从正式 Release Authority 在 `/opt/animemo` 重建非 secret application/deployment material；不能信任旧目录副本代替验证。
3. 把 protected configuration 原子转移到 data-root 的受保护 `config/`；不把 secret 留在新 app root。
4. 原子写入 versioned `instance.json`，记录 standard deployment profile 与 canonical roots。
5. 从相同 verified metadata 生成并安装精确 systemd allowlist/drop-in。
6. 执行 AniMemo-scoped reload、health check 和 release identity verification。
7. 任一步失败时恢复旧 profile、旧 locator/allowlist 与服务状态；保留 source tree 供 rollback。
8. 只有在 retention policy、rollback window 和 ownership evidence 均满足后，未来独立 cleanup 才可考虑旧 app tree。

cutover 明确禁止：

- 用 symlink 把旧 app root 或新 app root 伪装成另一方；
- 在 metadata 切换前删除旧 app tree；
- 移动、复制或清空已经正确位于 `/data/animemo` 的 data root；
- 重新执行历史 `/data/anime-journal` → `/data/animemo` migration；
- 因发现 legacy 变量名、dated 文档或 compatibility profile 而自动搬迁数据；
- 修改 1Panel、共享 OpenResty、DNS、TLS、防火墙或其他共享 VPS 组件。

`/data/anime-journal` 只允许出现在明确标注为历史记录的 evidence 中。Installer、Updater、Doctor 和 Migration discovery 都不得把它作为当前默认值或自动迁移触发器。

## 6. Backup / Restore / Migration / Export 边界

四个操作的语义不能合并：

- Backup：为同一 AniMemo instance 创建 disaster-recovery artifact；
- Restore：从有效 backup 恢复一个 instance；
- Migration：把 instance identity、数据与必要配置移动到另一环境；
- Export：生成用户拥有、面向可携带内容的数据，不等价于完整 instance recovery。

本文件只冻结 filesystem dependency，不实现这些操作。未来 Contract 必须显式列出每个子目录和 artifact 的 inclusion、checksum、metadata、encryption、retention 与 rollback 规则。

### 6.1 Database principle

PostgreSQL backup 的冻结基础是：

```text
pg_dump logical dump
+ checksum
+ metadata
```

禁止 tar、rsync 或复制 live `/data/animemo/postgres` 作为有效 database backup、restore 或 migration artifact。Restore 必须验证 checksum、metadata、数据库兼容性和目标 instance identity 后再导入。

## 7. Future migration dependencies

### 7.1 Migration Secret Envelope

Migration 必须保留恢复 encrypted credentials 所需的 `CREDENTIAL_ENCRYPTION_KEY`，但不得使用该 key 加密一个同时包含它自身的 bundle。未来 Secret Envelope 必须使用 external migration passphrase 或 one-time key、modern KDF 与 authenticated encryption。Phase 1 只冻结此依赖，不定义格式或实现。

普通 filesystem copy、`instance.json`、日志和非加密 migration manifest 都不得承载 secret。

### 7.2 R2 / remote media

source 与 target 使用同一 R2 bucket 时，Migration 不需要复制 poster/media bytes；应恢复 stable MediaObject references 及经 Secret Envelope 保护的必要配置。unknown R2 orphan 永远不得自动删除。Local media 仍按 `media/` 的 durable data 规则处理。

## 8. Future phase interfaces

- Installer：创建安全 roots、检测 foreign/partial install、写 protected config、原子写 `instance.json` 并协调 systemd allowlist。
- Updater：从 validated locator 发现 roots；只读 app material，只写被授权 state/data/runtime 区域；路径不一致时 fail closed。
- Backup：根据本文件的 inclusion classification 生成 logical database、local media、plugins、必要 config/state 及校验 metadata。
- Restore：验证空目标或兼容 existing instance、重建可重建 material、恢复 durable data，并重新生成 runtime state。
- Migration：理解 app/data roots 与 deployment profile，但不把 deployment binaries 当用户数据复制，不重跑 legacy path migration。
- Doctor：只读验证 locator、canonical paths、owner/mode、mount/write boundary、systemd allowlist、listen/public origin discovery 与 release identity，不显示 secret。

## 9. Current implementation compatibility evidence

截至 v1.1 Phase 1 authority baseline，当前实现已使用 `/data/animemo`、`/opt/animemo-updater`、`/var/lib/animemo-updater` 和 `/run/animemo-updater`；主要差异是 v1.0 app root 仍固定在 1Panel compatibility path。相关 current-state 文档为 [VPS Deployment](deployment-vps.md) 和 [Update Agent v1](update-agent-v1.md)。它们描述现有实例，不得覆盖本文件对 v1.1 新安装 target 和 future cutover 的规范。

本 Contract 的成功条件是边界冻结。它不授权本阶段创建 Installer、执行 cutover、移动生产数据、连接生产或开始 Backup/Restore/Migration/Doctor implementation。
