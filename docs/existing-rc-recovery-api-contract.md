# 原 RC 恢复的 API 与工具身份合同

PR 合并身份 GET 在 `release/recovery_pr.py` 内固定选择
`X-GitHub-Api-Version: 2022-11-28`。2026-03-10 已移除
`merge_commit_sha`；缺字段是合同不匹配，不能使用 main、HEAD 或 PR head
补齐。读取不接受环境/CLI 版本覆盖，也不自动回退。生产恢复与后续证明消费者
共用完整 PR 校验；其他受限 REST GET 显式选择 2026-03-10。证明命令仍使用
工作流固定的 gh 版本及原 release.yml/M1 签名主体。

V2 policy 保留原产品、计划、Tag、五资产和 revision 30 的身份，另绑定
`previousTool` R1（#255）以及已失败的 `supersededFailure` 34827731807。
新工具必须来自真实新 PR，且满足单父 R2 → R1 → M1、两层受审 tree，以及
checkout/run/workflow/main 的 R2 一致性。待绑定的 PR 编号拒绝运行；未来
合并 SHA、run ID 由平台读回，不写回源码。V1 原 policy 保存在只读历史夹具中，
没有执行回退路径。

本次授权时间固定于 2026-09-14T09:55:15.462Z，最多 24 小时。
scope 为 `ANIMEMO_V2_EXISTING_RC_SOURCE_BOUND_RECOVERY_API_FIX_V2`，
仅首个 execute、attempt 1；claim 前失败也消耗运行。完整工作流历史同时检查
旧失败事实与新运行，改标题或缺少 claim 不能恢复额度。

inspect 返回独立 inspection context，writeSteps 为空，不能用作 journal claim。
它使用只读 HTTP 开关和最低 Git 命令白名单 reader，不绑定发行发送或 journal
push 钩子。合并后的真实托管 inspect 必须先成功；execute 仍产生本运行自己的
最多 900 秒预检。既有原事务状态机、CAS 和每项最多一次实际发送规则继续适用。

`test_recovery_api_contract.py` 在最低 HTTP transport 捕获真实发出请求头，
通过生产 request、remote、platform、validator 测试两版真实 #255 响应和明确
标为 synthetic 的未来 R2。原 journal 回放只模拟发行远端结果，不绕过平台
身份解析。保留材料的完整字节及 inspect 检查通过
`ANIMEMO_RECOVERY_REPLAY_REAL_BYTES=1` 在本地执行；托管检查不伪造这些大文件。
真实发布与 Mirror 结论必须来自平台运行、公开证明和 Origin 回读，不能由测试
中的 revision 46 或模拟发送计数代替。
