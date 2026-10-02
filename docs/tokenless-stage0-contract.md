# 首信任与匿名 Stage-0 本地合同（DEVELOPMENT_ONLY）

本轮授权 ANIMEMO_V2_INITIAL_TRUST_DELIVERY_AND_TOKENLESS_TRANSPORT_LOCAL_V1。原 rc.3 / Release 392113678 / Q35444443071 保持原字节及 FAILED_PRESERVED 结论。前阶段报告和 v3 补丁保留；新源码必须重新取得资格。本文描述实现合同，验证与交付状态以本轮独立证据为准。

## 第一份信任

采用独立版本的 Bootstrap Trust Kit。操作者从既有官方维护者渠道选择固定受审源码、完整 entry.pyz 身份、清单身份和最低版本/序号，再用 OS 已信任下载与哈希工具核对入口。可信先决条件是维护者命名空间、操作者选择、Python 3.12 标准库、OS CA/主机名验证及时间；Sigstore 不证明其自身第一份验证器。

入口包含标准库 seed、清单及文件验证、HTTP worker 和进程监督模块。整体先由外置 pin 认证，随后按独立选择的 Release/资产 ID 获取清单与 kit。清单绑定源码 commit/tree、工具与依赖输入、平台、完整 tar 与每个成员、两类根、策略、期限及外置防回退下限。清单置于 tar 外，避免自摘要和未来 Q/merge 的循环依赖。

独立 kit 的 library/vendor/trust 来自固定受审构建输入，不能从待验产品取代码或动态库来运行。独立性来自先于产品的身份与来源，不来自目录名称、原 Q 标签或包内 checksum。seed 拒绝链接/reparse、重复路径、越界、特殊文件和超限，完整核对后保持文件句柄，再以 -I -S -B 新进程运行 kit。生产清单验证拒绝 DEVELOPMENT_ONLY；本轮 DEV runtime 只产生 TEST_ONLY 观察。

计划中的专用分发名称为 bootstrap-trust-kit-v1.0.0，状态 NOT_PUBLISHED，无真实 Release/资产 ID。本轮不会发布入口、修改系统信任目录或增加 rc.3 第六附件。kit 只允许操作者明确重新选择更新，最长 90 天且不晚于活动 TUF 根有效期；不自动使用 moving main/latest。

## 传输与进程

| 路径 | 合同 |
|---|---|
| 平台 reader | 固定仓库 API 2022-11-28，精确 tag/ref/peel 与唯一 github initiator inline release bundle |
| 公开产品资产 | 准确 Release ID、资产 ID、名字、size/digest；HTTP 200 或一次 302 至精确 GitHub release-assets host/path |
| Actions | 固定仓库 attestation API；必要 Azure blob 仅已有精确 host/path，查询串只走控制输入、不进 argv/诊断 |
| TUF 引导 | 原固定 GitHub/Sigstore 初始根 pin，官方域的连续 root、timestamp、snapshot、targets 与 trusted root |
| 管理 | 受控管理 gh/connector 与公开安装分开；本轮不改 registry 鉴权策略 |

公开安装不调用 gh release download 或 gh attestation verify，不读账户配置、token、Cookie、credential helper 或继承代理。GitHub 失败不会自动选 Mirror；显式 Mirror 仅运输，不授予首信任。

公开 GitHub/kit/TUF 请求通过同一标准库 HTTP worker。Windows 启动挂起子进程、归入任务 Job 后恢复，READY/GO 之后 DNS/TCP/TLS/headers/body/重定向共用单调截止。进程创建不是硬实时可中断保证。JSON/TUF 每请求最多 30 秒；平台 reader 总计 180 秒、32 MiB、16 页；JSON 8 MiB、TUF 16 MiB；资产 512 MiB、bundle 1 GiB、每对象 60–900 秒、bundle 1800 秒。重定向不刷新预算，无公开失败认证重试。

父进程仅在根进程回收、Job 为空、控制管道关闭后接受结果，并重算实际文件 size/hash。输出仍是未受信数据；HTTP 成功或 metadata.digest 不授予发行权威。超时、取消、异常协议和 kill/reap 未确认都不可消费；未知槽位保留。POSIX 实现与本机实测分别记录，Windows 不抵扣 Linux。

固定源码补齐与本地复验增量保留上述预算，并增加有界 PROGRESS 观察：最多 24 帧、合计 8 KiB，仍计入原 16 KiB 控制输出总量。记录实际响应状态、阶段和字节检查点；最后收到字节为观察下界，回收后暂存文件大小另列。未收到的状态保持 UNKNOWN，查询串、正文和私有路径不进入该记录。RESULT 终结协议；进度不能改变主错误、TUF 不存在语义或输出消费资格。当前成功关闭的 TUF 结果另存本轮报告，不补造旧 transcript。

## 密码学与消费

继续复用原 Go 源码及锁定密码库。平台 Release 使用 GitHub 私有根、精确 SAN https://dotcom.releases.github.com、唯一 GitHub, Inc. issuer organization 和一个可信 signed timestamp；不将 Actions 公共根用于平台签名。

Actions 使用同一固定 Go 的 actions-provenance 模式及公共 Sigstore 根，保持 SCT/Rekor/observer 与 issuer/SAN/workflow/ref、source/signer/config 三摘要。promote-release 的非 OCI 资产保留原执行 commit 观察语义，不拿 logical M 代替真实执行摘要。两个模式均使用私有副本、工具/root 摘要、输入重读、有限输出和受监督本地进程。

TUF 从原初始根验证连续迁移与阈值；初始/中间根和最终活动根期限分开。取到新 root 不等于合法更新。本地新 TUF 数据不覆盖操作者选定 kit 或系统信任状态。

完整 DEV 入口把已认证 Kit profile 的两个域版本作为刷新下界。原 Go 在真实时点完成验证后，每个角色版本可以保持或增长；同版本 root 和 trusted-root 目标必须保持原身份。原签名算法、初始根 pin 和根轮换规则不变。协议原件、实际 package/request/seed、真实 Go claim 与已验证下界保存在该次私有工作目录；下界先于六文件生成持久化，后续输出失败不撤销已验证事实。记录的验证起止区间不冒充 Go 库未导出的精确 RefTime，不保存 stderr、账户配置或任意响应日志。

生产 verify_for_production 仍限定 Linux root 和固定 /usr/share/animemo/stage0-trust/v1；生产 bootstrap 仍限定受保护材料、确切 VerifiedStage0Release 类型及一次性消费。DEV 使用不相容的 TestOnlyStage0Release，必须内部真实验签，再复用绑定及前后材料检查，仅返回内存 TEST_ONLY 提交观察。DEV 不生成生产能力或提交 PRIVILEGE_ALLOWED 文件。

产品代码必须在独立 kit 验证后由新的隔离产品进程加载，避免 kit 模块缓存冒充产品来源。现有运行时材料/路径门涵盖新增网络依赖；本轮不执行原产品安装，不放宽该门。新包进入材料构建、源码投影与安装复制闭包；涉及安装脚本变化时，既有 DEV 材料兼容门仍可要求重建，不能把原 Q 当成新源码资格。

## 原始归档与成员交接

`VerifiedReleaseMaterials.material()` 只查询实际声明的解压成员。外层 `installer-materials.tar` 通过 `GitHubReleaseSource.open_verified_release()` 的新鲜匿名事务交接，不从成员缓存或同名内层成员取得。

| 所有者 | 持有与结束时点 |
|---|---|
| source 事务 | 下载槽原 tar 经 receipt/实际字节核对后立即取得文件与目录持有，覆盖 Actions、解包、平台验证和最终消费 |
| 显式原归档借用 | 绑定同一 source、已验证成员对象与规范内容、实际 Release/asset、运输请求及文件对象；仅允许一次进入，前后从原持有句柄复读 |
| 平台/TEST_ONLY 消费 | 核对第二次实际平台元数据与首次下载来源一致；继续执行原双域密码学和一次性消费 |
| owner 退出 | 先撤销借用，再关闭持有、清理准确所属目录；失败保留原错误和受限次错误，未知资源保留 owner，不重试删除 |
| 最终结果 | 仅在事务退出成功后写成功记录；证据副本不授予产品或系统权威 |

Windows 使用既有拒写/拒删共享模式及目录持有；其他平台按原句柄与路径对象前后复核。修改、替换、截断、关闭、跨事务或二次借用被拒绝。成员、外层原字节与证据副本保持各自含义，不能通过重新打包成员替代原始 tar。

DEV 私有证据保存实际响应的必要安全 Release/asset 投影、原字节摘要与观察时间、五项公开 Actions 原件和平台 Release bundle。可在原持有关闭前复制一次完整原 tar，重验 size/hash 后仅标为公开资产证据；验证失败不能把该副本升级成可信产品。当前 TUF 下界、签名/身份策略及生产 loader 的 DEV 拒绝均保持。

## 本轮出口

显式出口只接受操作员提供的无凭据 loopback HTTP CONNECT 端点（规范 `http://127.0.0.1:端口` 或 `http://[::1]:端口`）。入口、隔离 runtime、监督器和 worker 用参数及配置摘要绑定同一选择；丢失或不匹配失败关闭。DIRECT 保留为明确默认，不读取环境代理、注册表或 `no_proxy`，代理失败不回退直连。离线 API 拒绝配置代理及实际网络读取。

CONNECT 仍由标准 HTTPSConnection 完成，TLS 的 SNI、默认 CA 与主机名检查使用原目标域。原目标/重定向白名单、30/900/1800 秒和 GO/Job 回收边界不变；401/403/407、异常隧道头、证书拒绝保留固定失败分类。安全 HTTP 观察可记录所选 loopback 端点、目标及实际隧道建立，不授予任何签名或安装权威，也不改变九字段 DEV 结果。入口 seed 的本地 Kit 选项只是材料来源；其后在线 runtime 不因此成为离线模式。

局部门 INITIAL_TRUST_DESIGN_AND_TOKENLESS_LOCAL_SEAMS_REVIEWED 仅在设计、组件、最终源码真实匿名 Windows 验证、独立复审及资源收口达到合同后使用。源码实现、loopback 或旧阶段成功不能触发此门。公开 kit 交付、匿名空机安装、Linux/VM、D2 和新 Q 未执行，下一阶段需独立受保护集成与交付授权。

参考：[TUF 规范](https://theupdateframework.github.io/specification/latest/)、[GitHub Release assets](https://docs.github.com/en/rest/releases/assets#get-a-release-asset)、[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
