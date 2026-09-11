# Guest sudo 会话控制器

`scripts/guest_sudo_session.py` 的 `SessionSupervisor` 负责固定 rotation / sudo validation；`scripts/candidate_guest_session.py` 将其接入完整 Candidate Provider，并提供独立的一次性 workload 交付。接口是进程内能力；没有从 JSON、ready marker、PID 或命令文件取得凭据的入口。

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

## 宿主会话密钥准备

`_prepare_profile_authority` 在 Clone 复制前，为当前私有 profile 生成新的 Ed25519 密钥对。固定的 `ssh-keygen` 与 `ssh`、`scp` 一样使用 OpenSSH 专用环境；`PROGRAMDATA` 来自已核验的 Windows Known Folder API，不能被父环境重定向。通用 VMware/复制命令仍使用独立环境。缺失该字段会让 Windows OpenSSH 在标准流初始化前退出，可能只留下 255 和空输出。

生成和公钥派生均走现有私有工具、身份检查及受控 launcher，显式传递空 passphrase。准备期间持有私有目录，生成后检查文件类型、大小、独占链接、属主/权限，并持有文件验证公私钥配对；验证成功后才允许进入 Clone 复制。公钥允许读取，但不能被不受信主体修改。失败、超时、取消或校验失败会清理本次新槽位中的两份密钥文件；一个删除失败也会继续尝试另一个，并阻止成功结论。既有 profile 冲突在生成前拒绝，不删除既有密钥。

会话密钥命令失败保留既有错误码，另输出固定工具名、失败类别、真实退出码、超时状态及 stdout/stderr 是否为空；启动/超时未取得的字段为 null。不输出命令、完整环境、标准流正文或私钥。此诊断不是 Guest 观察，也不消费或新增 sudo 捕获额度。

真实 Windows 准备回归入口为 `python -m unittest scripts.tests.test_windows_session_keygen`。它使用专用可丢弃的计划、模板和 bootstrap 测试数据，真实执行生产工具与准备逻辑，停止在 Clone 复制之前；不支持 Windows 时明确跳过。

## 单 Profile 动态入口

Windows execution 的私有工作根使用紧凑的 `session/profile/vm` 路径；Candidate、源码和完整 Clone digest 仍由 plan、active authority、lease 和材料绑定校验，不在目录中重复展开。VMware 可在文件实际存在时因过长 VMX 路径报“找不到虚拟机”。复制前检查所有目标文件路径的 UTF-16 长度，保留临时文件后缀余量；超预算在调用 VMware 前拒绝。宿主与 Supervisor 对 session/profile/ssh/Clone 目录统一使用 FILE_LIST_DIRECTORY 持有且不共享 DELETE，既拒绝替换，又允许 VMware 设置工作目录和内外两层同时持有。内层关闭不释放外层保护；仅 FILE_READ_ATTRIBUTES 不提供同样的替换保护。源、材料、工具的 holds 与所有私有 ACL 保持不变。

无凭据的 vmrun 恢复、启动和停止命令保留操作、Clone 路径、受信工具 digest、起止时间、真实返回码或 timeout/cancel/launch 分类。标准流每流最多匹配 16 KiB 中已知的通用错误摘录，精确 Clone 路径以占位符替换，未知内容不持久化；不扩展到 SSH/sudo 标准流。初始故障与 containment、清理、POSTSTATE 故障分别记录，后者不覆盖初始故障。未到成功启动阶段时，一次空运行清单只记 `NOT_RUNNING_OBSERVED`，不宣称启动后已软关机。

`python -B -m scripts.isolated_guest_validation` 是 `ANIMEMO_V2_ISOLATED_DYNAMIC_VALIDATION_AND_QUALIFICATION_V1` 的最小编排入口。必填参数为 `--verified-candidate-digest`、`--expected-qualification-run-id`、`--expected-source-sha`、`--expected-source-tree`、`--result`；结果路径必须尚不存在。执行前须已获得该任务的动态授权，并在最终 main 的干净 checkout 中取得同源 Qualification 的 canonical Verified Candidate。

入口在同一进程持有 provider execution、Candidate materials 与 profile lease，取得本次 R2 PRESTATE 后，只选择 FRESH_BASE，复用 harness 的全字节复制、Snapshot/磁盘图校验、challenge 和启动序列。密码捕获前先通过只读 canonical bootstrap 观察；捕获后由 Supervisor 重新验证目标并执行两个固定 role。入口不会生成 Candidate Profile/Aggregate Receipt。

`WindowsConsoleCapture` 要求未录制、可见且仅当前 Python 进程附着的原生 Windows Console，校验 Win32 console handle/mode，关闭 echo，以可变 UTF-16 缓冲读取后直接转换为可变 UTF-8 buffer。每次读取返回后也复核同一 Console。重定向、共享 Console、不可见 pseudoconsole、取消、模式变化和读取失败均关闭该捕获路径，没有明文输入 fallback。生产启动须让专用 Console 直接运行 Python，不能从会在结束后继续接收输入的交互 shell 调用。若清空输入队列失败，保持隐藏模式并终止此 Python/专用 Console；不得恢复 echo 后继续使用窗口。不要把密码放入聊天、命令、环境或结果文件。

输入反馈使用一个 Unicode 码点对应一个 `*`，UTF-16 代理对只显示一个掩码；退格同步移除最后一个掩码，包括缓冲区换行处。只将固定掩码和光标操作送往 Console，不回显密码原文。输入长度会通过掩码可见。预检要求原生输出启用 processed output 和立即换行，以保持掩码及光标一致；不支持的输出模式在捕获额度登记前拒绝。掩码写入或擦除失败同样终止本次捕获并清理缓冲区。

捕获前在固定的 `E:/<SHA256(CAPTURE_AUTHORIZATION)>` 私有目录原子登记一次尝试。此记录只阻止再次捕获，不提供 Guest authority；与 run/session/source SHA 无关，重启进程或创建新计划不能重置次数。取消和失败也保留记录，下一次真实捕获需新的授权处理，不删除该记录重试。

成功和失败都回收当前 SSH/secret/session key，并尝试软关机；provider 必要时使用既有 suspend containment。结果分别记录 STOPPED、SUSPENDED 或未完成 containment，suspend 不算正常关机。该入口保留本次 private-work 中的 Clone/测试数据，execution 退出优先清理复制的 bootstrap key，再清理工具/source 临时副本。独立清理步骤逐项执行，任何失败都会记录并阻止成功结论。随后取得新的 R2 POSTSTATE，并再次核对源码。报告只包含公开身份、操作类别、计数与收尾状态。

## 使用范围与后续任务

`SessionSupervisor` 成功后立即清理秘密。完整 Candidate 由 canonical `ClosedVmwareProvider.execute_profile` 调用 `bootstrap_candidate` 和 `execute_candidate_workload`；其 staging 与 runner 不再读取环境密码。Formal 保留独立的既有路径，不继承 Candidate 凭据额度。

### 完整 Candidate 的两用途额度

固定授权 `ANIMEMO_V2_EXACT_CANDIDATE_ACCEPTANCE_V1` 的计数根为 `E:/6d30583b353270ffd7b42163054ca7a470b74d5c9f6c19c920718364661ea82a`。每个固定 Profile 各有 `SESSION_BOOTSTRAP`、`CANDIDATE_WORKLOAD` 两个用途；根下两级目录分别为 Profile 和用途 ASCII 字节的 SHA-256，复用私有目录的 exclusive 创建与目录持有交接。每用途最多一次人工捕获，目录存在即已尝试。取消、无效输入、崩溃或部分交付不恢复额度；源码、session、Q 和新 Clone 不改变槽位。旧授权目录保持原样。

Bootstrap 在新的无密码 Guest 观察和 Console preflight 后占用槽位，复用两个既有角色，成功后由 Provider 登记绑定当前 execution、plan、Profile 和 lease 的进程内连接。workload 不接收外部 connection ticket；它对新观察重新比较 runtime、machine/boot ID、MAC、challenge 与已认证 host key。正常续接允许使用本 Profile 已登记的 key，跨 Profile/session 的 freshness 检查和已使用 key 集合保持有效。

材料先通过无秘密 SCP 传到当前 session/Profile 独占 staging。Host 验证受持有材料和受审 root 程序字节，重新观察 Guest 后才预检并占用 workload 槽位。最终观察来自随后接收秘密的同一 SSH 子进程。一个固定 sudo 子进程执行 `candidate_workload_root.py`：逐级 no-follow directory fd 复制，拒绝 symlink、hardlink、特殊文件、目录替换、文件增长和目标预占；新目标从创建时即 root-owned，复制后失去写权限。其完整库存与宿主持有摘要相等后，才从已验证目标加载 wheel runtime 和 canonical Profile Runner，并安全读取固定 Profile Draft。

宿主和远端转发器在一次 write/flush/close 后立即清理可变密码，不等待安装完成。短写不补写；grant 的五秒窗口与固定工作负载最长五小时（含材料完结）的进程预算分开。stdout 只包含有界身份观察与回执，由宿主持续读取，Runner 普通输出不进入该通道。SSH 超时或断线进入 Clone containment，不认为远端 root 已退出，不重新交付密码。Provider 退出 Profile 时撤销连接登记。

公开记录按 Profile、用途与角色分别保存 capture/delivery attempts、completed 和操作结果，并绑定 plan/session/source/tree/Q/Candidate。完成交付不等于安装 PASS；只有 canonical Guest Draft、Host 模板前后观察及最终收据验证均通过，才支持对应结论。

已获得本固定任务授权时，专用原生 Console 可直接运行 `python -B -m scripts.candidate_vm_harness --execute --authorization-id ANIMEMO_V2_EXACT_CANDIDATE_ACCEPTANCE_V1 --r2-origin-transport cloudflare-plugin`，同时提供准确的四项 Candidate/Q/source 参数与尚不存在的 `--result` 路径。该授权接受本进程产生并保存的完整 plan；不是后续任务或发布权限。

离线检查运行 `python -m unittest scripts.tests.test_guest_sudo_session`，全部输入为 synthetic sentinel 和 mock Guest transport；Windows 用真实私有文件/目录 holds 检查生产 scope。Linux 跳过仅 Windows ACL 的测试；原 harness 回归另行保留。结果最多支持 `OFFLINE_SECURITY_REVIEW_PASSED_DYNAMIC_PENDING`，不能写成 Candidate PASS、真实 Guest 安全或 Snapshot 已修复。

新增权限的定点测试为 `scripts.tests.test_candidate_guest_session`、`scripts.tests.test_candidate_workload_root` 和 `scripts.tests.test_candidate_plugin_acceptance`。前者在 Windows 使用真实 holds 与本机子进程；POSIX fd 对抗测试在 Linux 执行。`scripts/tests/native_candidate_console_probe.py` 必须在独占可见 conhost 中显式启动，只自动输入公开测试文本并验证退格、取消及两用途的原生通道；它不访问真实额度，不执行 VM 或 sudo。所有这些都是开发证据，不能替代真实三 Profile 验收。

每次实际执行仍须有适用的明确授权，先比对最终 main、控制器源码与 Qualification，再核对模板、Profile、Snapshot 和任务资源；成功 Q 只绑定其准确源码，历史结果不能授权新源码。
