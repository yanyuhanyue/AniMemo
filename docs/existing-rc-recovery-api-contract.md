# 原 RC 恢复的 API 与工具身份合同

PR 合并身份 GET 在 `release/recovery_pr.py` 内固定选择
`X-GitHub-Api-Version: 2022-11-28`。2026-03-10 已移除
`merge_commit_sha`；缺字段是合同不匹配，不能使用 main、HEAD 或 PR head
补齐。读取不接受环境/CLI 版本覆盖，也不自动回退。生产恢复与后续证明消费者
共用完整 PR 校验；其他受限 REST GET 显式选择 2026-03-10。证明命令仍使用
工作流固定的 gh 版本及原 release.yml/M1 签名主体。

V2 policy 保留原产品、计划、Tag、五资产和 revision 30 的身份，另绑定
`previousTool` R1（#255）、`parentTool` R2（#256）、`diagnosticTool` R3（#257）、
`draftReadTool` R4（#258）及已失败的 `supersededFailure` 34827731807。
R5 必须来自本次受审的新 PR，满足单父 R5 → R4 → R3 → R2 → R1 → M1，
逐跳绑定真实 PR、受审 head/tree、仓库/分支和合并身份，以及
checkout/run/workflow/main 的 R5 一致性。待绑定的 PR 编号拒绝运行；未来
合并 SHA、run ID 由平台读回，不写回源码。V1 原 policy 保存在只读历史夹具中，
没有执行回退路径。

本次授权时间固定于 2026-09-14T09:55:15.462Z，最多 24 小时。
scope 为 `ANIMEMO_V2_EXISTING_RC_SOURCE_BOUND_RECOVERY_API_FIX_V2`，
仅首个 execute、attempt 1；claim 前失败也消耗运行。完整工作流历史同时检查
旧失败事实与新运行，改标题或缺少 claim 不能恢复额度。必须存在准确 R3/R4
失败 inspect；三次新增 inspect 上限跨工具共享，原 R2 inspect 不占该补充计数。

inspect 返回独立 inspection context，writeSteps 为空，不能用作 journal claim。
它使用只读 HTTP 开关和最低 Git 命令白名单 reader，不绑定发行发送或 journal
push 钩子。R5 的明确窄例外仅将 inspect job 的 Contents 设为 write，其余列明
权限保持 read、未列权限为 none；全局权限保持 read。该原生短期令牌在平台层
具有 Contents 写能力，不能声称 IAM 仅允许两个 Draft，也不构成 OS 沙箱。
发行写方法、claim、journal push 与不在固定白名单内的子进程在发送前拒绝；
checkout 保持 persist-credentials:false，inspect 不配置 journal credential helper。

固定 Draft、Release 发现分页、事务 assets 与已验证资产 GET 使用各自 job 的
原生 GITHUB_TOKEN；ADMIN_READ 仅用于 immutable/protection 设置读取。
401/403 不触发凭据或版本回退。受限解析的权限要求头、数字限流头与实际状态
仅作为新请求诊断，不能证明令牌实际权限，也不能倒填旧 UNKNOWN 或 403。
平台 job 权限元数据不可用时记录 UNKNOWN，由实际运行日志另行核对。

同一运行先通过平台/工具身份，再验证两份固定 Draft、唯一发现分页和空资产
列表，然后继续原材料、journal、Tag/registry、五原证明及完整 recovery precheck。
早期读取成功只产生固定草稿观察，不产生完整 inspect 成功或可写 claim。
合并后的真实托管 inspect 必须先成功；execute 仍产生本运行自己的
最多 900 秒预检。既有原事务状态机、CAS 和每项最多一次实际发送规则继续适用。

`test_recovery_api_contract.py` 在最低 HTTP transport 捕获真实发出请求头，
通过生产 request、remote、platform、validator 测试两版真实 #255 响应和明确
标为 synthetic 的 R5 工具事实。原 journal 回放只模拟发行远端结果，不绕过平台
身份解析。保留材料的完整字节及 inspect 检查通过
`ANIMEMO_RECOVERY_REPLAY_REAL_BYTES=1` 在本地执行；托管检查不伪造这些大文件。
真实发布与 Mirror 结论必须来自平台运行、公开证明和 Origin 回读，不能由测试
中的 revision 46 或模拟发送计数代替。
