# 托管发行读取与冻结版本占用

正式 Draft、五项资产和 Publish observer 共用 `GitHubReleaseDiscovery`。读取组件只决定当前远端观察结果；发布计划、资格、Freshness、事务与操作员授权继续由各自的入口验证。

## 原生 job 的读取范围

`HostedGitHubReadClient.from_current_job()` 只接受受审的三个入口：专用 `read-contract`、RC Publish 和 Stable Promote 的既有发布 job。它使用平台注入的 `GITHUB_TOKEN`，核对准确仓库数值 ID、操作员、attempt 1、当前 run/job、checkout、工作流原件与事件。PR 入口还核对同仓非 fork、当前 main、准确 PR head 及操作员维护的已审 head 标记；正式发布使用准确 main 的 dispatch。

这些检查依赖受审工作流和同 job 代码的完整性。Python 对象、环境变量和 GET allowlist 都不是抵抗同进程恶意代码的安全沙箱，也不能从 token 字符串证明它由 GitHub 签发。平台提供 token 的事实来自受审入口；实时 API 回读验证其运行来源，当前对照草稿验证实际可见性。任意本地 `GH_TOKEN`、caller 布尔值或自写 receipt 都不能获取这条正式托管路径。

job 初始化日志中实际报告的 Token Permissions 在运行后单独取证。API 不提供有效权限字段时不自行补造。`permissions.push=false` 保留原值；端点的 Accepted Permissions 也不当作 token 实际权限。

## 有界发现

一个观察窗口最多 60 秒：准确仓库、普通控制草稿按 ID 的新基线、by-tag、两次完整列表、控制草稿再次按 ID 回读。控制对象固定为普通 Draft `373784357`，但 `updated_at` 和正文在每次新窗口建立基线，允许获准合并触发 Drafter 正常更新。窗口内基线或集合漂移则拒绝；两次稳定观察不表示 GitHub 提供了事务快照。

目标唯一时按真实 ID 回读，并完整枚举该 ID 的资产；没有目标时，只有上述托管范围、完整列表及前后对照同时通过才返回 ABSENT。401/403/429/5xx、无响应、无控制对象、错类型、重复项、分页或传输不完整均保持 UNKNOWN。只有合格原生客户端可共享窗口缓存；身份变化、超时和每次 mutation 前后都会使缓存失效。创建 Draft 前重新观察，冲突或不确定响应交给既有事务回读，不自动重发 POST。

正式上传使用已经核验的 Release ID 和固定 uploads.github.com 路径；不通过 tag 再发现目标。下载核对准确仓库/资产 ID URL，跨 GitHub 下载域重定向时移除 Authorization。API 请求保持有界字节、HTTP framing 和重复 JSON 键校验。

## 负向冻结占用

`FROZEN_UNATTEMPTED_VERSION_OCCUPANCY` 仅排除已冻结编号，不能生成 Candidate PASS、签名或发布许可。源码记录绑定真实 rc.2 的 M/T/Q/P、operation、ref/head、revision 1/FROZEN、原 ledger 摘要及 17 步无业务尝试的状态；决定来源是操作员批准的独立保留占用合同。

resolver 先枚举真实 publication transaction refs，使用 canonical journal loader 验证线性历史，再核对记录、原字节与稳定 ref inventory。所有未解释的非终态事务继续阻断。发布控制器在打开 journal 前额外检查新计划的 remoteKey 不能碰到被冻结的远端键；原版本不可复用。现有部分发布 reservation 的签名要求保持不变。

新 rc.3 发布计划携带 `predecessor_frozen_occupancies`，明确前驱 rc.2 未发布且仍为 FROZEN。本次只读核验不追加、解冻或删除原 journal。资格、候选和未来发布均使用 canonical 版本结果；版本不一致时失败。

## 专用验证入口

`release-readback-check.yml` 替代已经用完的临时 rc.2 诊断入口。本次检查绑定新授权及其 48 小时窗口，允许同仓 #263 的已审 `ready_for_review` 或合并后的 main 手动读取；额度由操作员执行记录控制，不因 rerun 恢复。专用 job 的 Contents write 是平台草稿可见性能力，实际业务请求由受审代码限定为 GET；Artifact 上传另行计数。它没有 registry、OIDC 或 attestations 写权限。

检查用合成比较值调用正式七个 observer，只允许当前 rc.3 全部 ABSENT 的结果通过；合成值不是新候选材料。结果仅保存阶段、公开身份、分类和白名单 HTTP 元数据，固定私有目录排他创建。生产原始正文、token、签名 URL 和异常内容不进入输出。

本地回归使用合成 HTTP/job 响应，以及明确标注为离线副本的真实 rc.2 两快照；它们验证代码，不替代实时托管结果或正式 Candidate。
