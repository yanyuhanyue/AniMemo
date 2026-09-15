# Candidate 与本地开发批次的分级失败策略

策略身份：`animemo.graded-profile-failure/v1`。此策略向前生效；历史结果保持原 schema、原源码、原执行事实。历史首败未关闭的合同差额不会因本策略获得追认。

一个已确认批次始终只有一次人工密码捕获。确认前的 plan、原生确认正文、完整结果与新 Candidate Aggregate 均记录相同策略身份；具体标识只在运行时绑定，不把某次任务、PR 或 Qualification 写入产品白名单。

## 可信业务失败

只能在固定 Installer/平台失败代码、受验证诊断阶段和退出码一致时提出暂定分类。必须已进入准确 root、材料和 runtime 验证完成，Installer 已执行；当前固定链的 Runner/root/sudo 返回 2，Installer 返回自身合同定义的业务失败码。协议错误、缺失阶段、未知错误或冲突退出码不能进入此分支。

APT 的深层错误文本为 UNKNOWN，并不等于密码交付未知。完整 APT 观察保留真实 returncode；TIMEOUT/CANCELLED/LAUNCH_FAILED、缺失输出、次要进程/诊断错误，以及签名、证书、来源、磁盘或锁风险均拒绝业务续跑。截断标记本身不能被用来伪造完整输出，也不等于秘密交付不完整。

诊断 EOF 仅是暂定证据。Supervisor 必须等实际 SSH 子进程退出、关闭宿主受控子进程树，核对完整一次交付、退出码与准确 Guest/连接/租约/源码；退出冲突或短写立即撤销 owner。关闭 Supervisor 后，临时 grant、局部秘密和 BatchUse 必须已关闭。

Provider 然后检查准确副本 STOPPED、会话 key/known_hosts 删除、全部资源持有关闭、lease 释放、原源码未变。SUSPENDED 或仅观察不到运行均不足以授权业务续跑。所有 cleanup 完成后才重新取得 continuation 检查，生成可继续的本地异常。任何 cleanup 错误立即撤销 owner，同时保留最初业务失败与全部次要错误。

Candidate/DEV 循环必须消费 `validate_business_continuation(provider, plan, profile, error)` 的准确结果；不能按错误字符串或 `revoke_batch=False` 直接续跑。失败 Profile 保持 ERROR/FAIL，角色不重发；仍能执行其余独立 Profile 也不会使 Aggregate 从 FAIL 变 PASS。

## 安全或执行身份不确定

认证、来源/材料/Guest/租约漂移、部分或未知交付、协议损坏、超时/取消、未知活动 root、containment 或 cleanup 未完成，均立即撤销整个 owner/batch。后续 Profile 为 NOT_RUN_SHARED_BLOCKER，不为等待 Origin POSTSTATE 或报告写入保留秘密。所有实际撤销都同步关闭开发内存 owner；不再使用宽泛的通用错误作为暂存秘密理由。

## Aggregate 与消费者

当前插件生产路径输出 `animemo.prepublication-candidate-acceptance-receipt/v5`，强制包含 `failure_policy`；该字段和 Profile/Origin/plan/session 绑定都进入 canonical 自摘要。v3/v4 仍可读取、解码和核验其原有合同，用于保留历史证据。当前 Freshness Candidate loader 和 Publish Candidate 子消费者只接受匹配当前策略的 v5 且全部 Profile PASS，不把旧 v4 自动改写成新执行结果。

本地合成的完整回执和 transport 回归始终是 NON_AUTHORITATIVE_LOCAL_REGRESSION；不是正式 Candidate。开发报告保持 DEVELOPMENT_ONLY，不产生 Candidate Profile Receipt、正式 Aggregate 或发行 authority。

## 通用本地开发入口

使用 `python -m scripts.seams_development_controller`，`--authorization-id` 必填；其余准确材料/源码参数按当前入口 `--help` 和已验证材料计划填写。`--platform-diagnostic` 是可选固定模式，仅运行一个全新 Fresh 副本的受控平台诊断；不运行应用 Installer/Doctor/CRUD。省略该参数运行完整干净预验收。原生确认使用一个 round，`round_limit=1`，仍须操作员本人在本机窗口确认并在原生 Console 输入一次秘密。

诊断模式的 APT 操作按开发 Guest 写入计数。诊断报告可为 PASS 或 FAIL；成功传输一个 FAIL 报告时 Runner/root/sudo 可返回 0，这表示报告运输完成，报告内真实 APT returncode 和 FAIL 不变。专用 validator 必须绑定模式、源码、Guest/上下文和观察，常规 Candidate 路径不接受这类报告。纯平台诊断的 owner 收尾原因是 DEVELOPMENT_PLATFORM_DIAGNOSTIC_COMPLETED；它不能替代 DEVELOPMENT_PREACCEPTANCE_PASSED。

无凭据回归入口包括 `scripts.tests.test_candidate_failure_policy`、`scripts.tests.test_graded_supervisor`、`scripts.tests.test_candidate_profile_cleanup`、`scripts.tests.test_candidate_plugin_acceptance`，覆盖真实本机子进程配合合成秘密、分级判断、最终清理、即时撤销和完整回执消费者。测试模拟的 Guest/VM/材料不是真实预验收；真实开发验证须保持同一冻结源码与材料，改代码前先关闭现有秘密会话。
