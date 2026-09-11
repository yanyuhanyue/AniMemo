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

`delivery_attempts` 按两个固定 role 记录首次 stdin 写入前的尝试数；`delivery_completed` 与总计 `injection_count` 只计完整 write/flush/close。短写和部分写入异常都终止会话且不补写，不能因完成数为零而断言密码未送出。完整交付也不等于 sudo 操作成功，操作结果单独记录。

## 单 Profile 动态入口

`python -B -m scripts.isolated_guest_validation` 是 `ANIMEMO_V2_ISOLATED_DYNAMIC_VALIDATION_AND_QUALIFICATION_V1` 的最小编排入口。必填参数为 `--verified-candidate-digest`、`--expected-qualification-run-id`、`--expected-source-sha`、`--expected-source-tree`、`--result`；结果路径必须尚不存在。执行前须已获得该任务的动态授权，并在最终 main 的干净 checkout 中取得同源 Qualification 的 canonical Verified Candidate。

入口在同一进程持有 provider execution、Candidate materials 与 profile lease，取得本次 R2 PRESTATE 后，只选择 FRESH_BASE，复用 harness 的全字节复制、Snapshot/磁盘图校验、challenge 和启动序列。密码捕获前先通过只读 canonical bootstrap 观察；捕获后由 Supervisor 重新验证目标并执行两个固定 role。入口不会生成 Candidate Profile/Aggregate Receipt。

`WindowsConsoleCapture` 要求未录制、可见且仅当前 Python 进程附着的原生 Windows Console，校验 Win32 console handle/mode，关闭 echo，以可变 UTF-16 缓冲读取后直接转换为可变 UTF-8 buffer。每次读取返回后也复核同一 Console。重定向、共享 Console、不可见 pseudoconsole、取消、模式变化和读取失败均关闭该捕获路径，没有明文输入 fallback。生产启动须让专用 Console 直接运行 Python，不能从会在结束后继续接收输入的交互 shell 调用。若清空输入队列失败，保持隐藏模式并终止此 Python/专用 Console；不得恢复 echo 后继续使用窗口。不要把密码放入聊天、命令、环境或结果文件。

输入反馈使用一个 Unicode 码点对应一个 `*`，UTF-16 代理对只显示一个掩码；退格同步移除最后一个掩码，包括缓冲区换行处。只将固定掩码和光标操作送往 Console，不回显密码原文。输入长度会通过掩码可见。预检要求原生输出启用 processed output 和立即换行，以保持掩码及光标一致；不支持的输出模式在捕获额度登记前拒绝。掩码写入或擦除失败同样终止本次捕获并清理缓冲区。

捕获前在固定的 `E:/<SHA256(CAPTURE_AUTHORIZATION)>` 私有目录原子登记一次尝试。此记录只阻止再次捕获，不提供 Guest authority；与 run/session/source SHA 无关，重启进程或创建新计划不能重置次数。取消和失败也保留记录，下一次真实捕获需新的授权处理，不删除该记录重试。

成功和失败都回收当前 SSH/secret/session key，并尝试软关机；provider 必要时使用既有 suspend containment。结果分别记录 STOPPED、SUSPENDED 或未完成 containment，suspend 不算正常关机。该入口保留本次 private-work 中的 Clone/测试数据，execution 退出优先清理复制的 bootstrap key，再清理工具/source 临时副本。独立清理步骤逐项执行，任何失败都会记录并阻止成功结论。随后取得新的 R2 POSTSTATE，并再次核对源码。报告只包含公开身份、操作类别、计数与收尾状态。

## 使用范围与后续任务

本模块实现最小控制器的 rotation / sudo validation，不负责启动或复制 VM、不实现完整 Candidate/Formal workload 调度，也不为后续 privileged workload 提供 env fallback。本次没有启用历史环境变量式 sudo 入口。动态任务应在受控 profile 入口使用本模块；不得退回历史外置 `capture_and_serve` 或从旧 authority JSON 请求 secret。

离线检查运行 `python -m unittest scripts.tests.test_guest_sudo_session`，全部输入为 synthetic sentinel 和 mock Guest transport；Windows 用真实私有文件/目录 holds 检查生产 scope。Linux 跳过仅 Windows ACL 的测试；原 harness 回归另行保留。结果最多支持 `OFFLINE_SECURITY_REVIEW_PASSED_DYNAMIC_PENDING`，不能写成 Candidate PASS、真实 Guest 安全或 Snapshot 已修复。

下一次 VM/Guest/sudo/Qualification 执行需新的明确授权，先比对最终 main 和本模块/launcher 摘要，再核对精确模板、profile、Snapshot 单变量方案与任务自有资源。不得把旧 run33627874404 或历史 RC19 变成新源码资格。
