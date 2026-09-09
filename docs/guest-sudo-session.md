# Guest sudo 会话控制器

当前实现为 `scripts/guest_sudo_session.py` 的 `SessionSupervisor`，替代历史仓库外 RC19 controller 的 worker secret 分发边界。接口是进程内能力；没有从 JSON、ready marker、PID 或命令文件取得凭据的入口。

## 受控调用边界

调用方须先在 canonical `ClosedVmwareProvider.execution_authority()` 内取得真实源码 VM snapshot、readiness、HeldCandidateMaterialAuthority 与当前 CandidateHarnessPlan，并持有当前 profile 的 ProviderSessionLease。plan 不授予凭据使用权。控制器还验证 profile/clone/nonce、session、source SHA/tree、candidate version/input/verified digest、Qualification run、source snapshot、lease 生命周期及执行实例。

控制器接收一次捕获的可变内存 buffer（构造成功后归控制器所有）；调用方使用 context manager 收尾。构造失败时，调用方仍负责清理输入 buffer。不从 argv/environment/文件读取 sudo 值，不提供 worker IPC，也不替代操作员的动态授权。Python 内存清理是 best effort，不承诺消除解释器或 OS 的所有副本。

它在现有 profile 的受保护目录和 session key/lease 文件上持有 Windows authority；known-hosts 持有仅跨本次 SSH 连接，避免跨 host-key rotation 保留过时文件。

操作顺序：

1. `bootstrap_rotation()` 复用 provider 的只读 bootstrap gate，继而在同一 SSH 子进程中再次取得 Guest observation；真实 canonical bootstrap verifier 通过后，仅本次 BOOTSTRAP_ROTATION grant 可发送一次 secret，执行固定 session-key/host-key rotation。
2. `validate_verified_guest()` 取得旋转后的 key，通过完整 canonical verifier（含当前已使用 key 集合）后，才向产生该 observation 的同一进程 stdin 执行固定 sudo validation；成功后登记 key、清空 secret。
3. 失败、超时、取消、租约/源码/材料失效、运行 VMX 竞争、Guest 或进程身份变化均撤销会话；不会回到再次捕获或再次验证旁路。grant 不可序列化和重用。

实际进程由原 provider `_run` 的工具身份、固定 cwd、净化环境、active execution 前后检查启动；新增交互方法不绕开既有 launcher。stdout 仅接收有界 public observation，sudo 输出被丢弃；进程异常时关闭管道并回收本次子进程。

## 使用范围与后续任务

本模块实现最小控制器的 rotation / sudo validation，不负责启动或复制 VM、不实现完整 Candidate/Formal workload 调度，也不为后续 privileged workload 提供 env fallback。本次没有启用历史环境变量式 sudo 入口。动态任务应在受控 profile 入口使用本模块；不得退回历史外置 `capture_and_serve` 或从旧 authority JSON 请求 secret。

离线检查运行 `python -m unittest scripts.tests.test_guest_sudo_session`，全部输入为 synthetic sentinel 和 mock Guest transport；Windows 用真实私有文件/目录 holds 检查生产 scope。Linux 跳过仅 Windows ACL 的测试；原 harness 回归另行保留。结果最多支持 `OFFLINE_SECURITY_REVIEW_PASSED_DYNAMIC_PENDING`，不能写成 Candidate PASS、真实 Guest 安全或 Snapshot 已修复。

下一次 VM/Guest/sudo/Qualification 执行需新的明确授权，先比对最终 main 和本模块/launcher 摘要，再核对精确模板、profile、Snapshot 单变量方案与任务自有资源。不得把旧 run33627874404 或历史 RC19 变成新源码资格。
