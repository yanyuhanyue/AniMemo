# AniMemo

AniMemo 保存人与动画之间的长期记忆，并把承载这些记忆的自托管实例视为需要长期维护的产品对象。

## Durable Deployment

**AniMemo Instance**:
一套具有独立身份、配置和持久数据的 AniMemo 安装；同一软件版本的两个安装仍是不同实例。
_Avoid_: Site, deployment directory, Compose project

**Deployment Boundary**:
AniMemo 与自托管管理员之间关于安装、运行和公网暴露的责任分界。
_Avoid_: Managed hosting, panel integration

**Release Authority**:
唯一能够把可安装版本绑定到不可变来源、校验信息和精确运行 artifact 的发布权威。
_Avoid_: Latest, download server, mirror authority

**Bootstrap Endpoint**:
只负责传输安装引导程序、不能决定 release identity 的入口。
_Avoid_: Release source, stable authority

**Application Root**:
实例中存放可重新取得的应用与部署材料的根位置。
_Avoid_: Data root, backup root

**Data Root**:
实例中存放持久配置、secret 和用户状态的根位置。
_Avoid_: Application root, updater state root

**Instance Metadata**:
让 AniMemo 工具识别同一实例及其 roots、application identity 和 release identity 的版本化非 secret 记录。
_Avoid_: Environment file, release manifest

**Listen Endpoint**:
AniMemo 在宿主机接受本地或显式直接连接的网络地址。
_Avoid_: Public Origin, domain

**Public Origin**:
用户通过浏览器访问某个 AniMemo Instance 的 canonical external origin，也是需要绝对 URL 的应用身份来源。
_Avoid_: Listen address, proxy upstream, DNS record

## Data Portability

**Backup**:
为同一 AniMemo Instance 的灾难恢复保存的可验证副本。
_Avoid_: Export, Migration Bundle

**Restore**:
从 Backup 重建一个 AniMemo Instance 的恢复行为。
_Avoid_: Import, Migration

**Migration**:
把一个 AniMemo Instance 移动到另一运行环境，同时保持实例连续性的行为。
_Avoid_: Restore, Export

**Export**:
面向用户的数据可携带副本，不承诺重建完整 AniMemo Instance。
_Avoid_: Backup, Migration Bundle
