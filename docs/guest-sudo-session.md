# Guest sudo 会话控制器

`scripts/guest_sudo_session.py` 的 `SessionSupervisor` 负责固定 rotation / sudo validation；`scripts/candidate_guest_session.py` 将其接入完整 Candidate Provider，并提供独立的一次性 workload 交付。接口是进程内能力；没有从 JSON、ready marker、PID 或命令文件取得凭据的入口。

## 受控调用边界

调用方须先在 canonical `ClosedVmwareProvider.execution_authority()` 内取得真实源码 VM snapshot、readiness、HeldCandidateMaterialAuthority 与当前 CandidateHarnessPlan，并持有当前 profile 的 ProviderSessionLease。plan 不授予凭据使用权。控制器还验证 profile/clone/nonce、session、source SHA/tree、candidate version/input/verified digest、Qualification run、source snapshot、lease 生命周期及执行实例。

完整 Candidate 的 `CandidateBatch` 持有一次捕获的可变内存 buffer；每个 Supervisor 只接收绑定当前执行实例、plan、Profile、lease 和固定角色的 `BatchUse`。单 Profile 动态验证入口独立持有并清理其 buffer，不提供完整 Candidate 的替代入口。不从 argv/environment/文件读取 sudo 值，不提供 worker IPC，也不替代操作员的动态授权。Python 内存清理是 best effort，不承诺消除解释器或 OS 的所有副本。

它在现有 profile 的受保护目录和 session key/lease 文件上持有 Windows authority；known-hosts 持有仅跨本次 SSH 连接，避免跨 host-key rotation 保留过时文件。

操作顺序：

1. `bootstrap_rotation()` 复用 provider 的只读 bootstrap gate，继而在同一 SSH 子进程中再次取得 Guest observation；真实 canonical bootstrap verifier 通过后，仅本次 BOOTSTRAP_ROTATION grant 可发送一次 secret，执行固定 session-key/host-key rotation。
2. `validate_verified_guest()` 取得旋转后的 key，通过完整 canonical verifier（含当前已使用 key 集合）后，才向产生该 observation 的同一进程 stdin 执行固定 sudo validation；成功后登记 key、关闭该用途的 grant；Candidate 总缓冲由 batch owner 管理。
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

## 完整 Candidate 的单次输入

固定授权为 `ANIMEMO_V2_CANDIDATE_WORKLOAD_DIAGNOSTICS_SINGLE_CAPTURE_V1`，账本根为 `E:/78b1ae1ff8f196aa4cb31141423e33365e9334e34ee83735377a051053919901`，即授权 ID ASCII 字节的 SHA-256。目录 exclusive 创建消费一次人工捕获尝试；取消、无效输入、崩溃和部分交付均不恢复额度。账本独立于 run、session、Profile、Q 和源码。旧授权目录及其已消费记录保留。

网关根因修复后的新会话另识别固定范围
`ANIMEMO_V2_CANDIDATE_GATEWAY_REPAIR_SINGLE_CAPTURE_V1`，账本同样由该 ID 的
ASCII SHA-256 派生到 `E:/`。入口只接受代码中列明的三个固定 ID，显式传给 batch；
缺失 ID 直接拒绝。代码识别新 ID 不代表已取得新输入授权，必须先完成修复合并、同源新 Q 并获得
操作员对下一次新会话的一次输入短授权。准备或 plan-only 不创建新账本，首次合法
bootstrap 观察后才原子登记。每个范围各自最多一次尝试，均不能因换源码、Q、计划、
控制器或重启恢复额度；不接受任意 ID、路径或序号。旧记录保持原状。

一个控制器进程中的 `CandidateBatch` 绑定冻结 M/T/Q/Candidate 与 canonical plan，只允许 FRESH_BASE、DOCKER_BASE、RUNTIME_BASE_OFFLINE 串行执行。第一次无密码 bootstrap 核验、源码检查及 Console preflight 通过后捕获一次。每 Profile 的 BOOTSTRAP_ROTATION、VERIFIED_SUDO、CANDIDATE_WORKLOAD 各最多一次交付尝试，整场上界九次。前置角色未成功或前一 Profile 未结束时不得申请后续用途。

每次 Supervisor 仍执行完整目标、VMX、Snapshot/磁盘图、Guest challenge、host key、lease、源码和材料检查，并在同一 SSH 进程内完成观察与交付。跨 Profile 使用独立 key、known_hosts、Clone 身份；同 Profile 续接使用已登记的当前 key，不清空跨 Profile 的已使用 key 集合。`BatchUse` 没有 get_secret、任意命令或序列化接口；workload 的 `execute()` 不接受命令参数。

每次交付从 owner 借出临时可变 buffer，一次 write/flush/close 后立即清理。远端 forwarding buffer 同样在等待子进程前清理。正常 role/Profile close 仅关闭用途能力；第九次交付完成即提前清理 owner，其他结束路径在进入 POSTSTATE 和报告前清理。完整 write 不证明 sudo 或 Installer 成功。

捕获完成起使用单调时钟，硬上限 12 小时；没有登记操作时空闲上限 30 分钟。准备、bootstrap、传输、workload、清理均有固定超时，真实在途操作暂停空闲计时但不延长硬上限。到期、取消或控制器退出撤销全 batch、终止本任务子进程并执行 canonical Clone containment。会话不可续期、保存、重启恢复或自动重捕获；不得持有真实秘密编辑源码或等待 CI。

SCP/SSH/keygen/复制命令的后代也属于取消范围。Windows 在子进程仍处于 CREATE_SUSPENDED 时加入进程内匿名 Job，再校验并恢复该子进程唯一初始线程；关闭 Job 回收其后代，没有先启动再登记的空窗。POSIX 使用独立进程组。vmrun 命令不加入会强杀后代的 Job，虚拟机仍只能经过 canonical soft-stop/suspend 收尾。执行入口必须携带当前授权 ID 和结果路径，单独接受 plan digest 不能消费新额度。

材料经无秘密 SCP 进入 session/Profile 独占 staging。Host 比对受持有材料和受审 root 程序，固定 sudo/root 程序通过 no-follow directory fd 复制，拒绝链接、特殊文件、目录替换、增长和目标预占；新 root-owned 目标封闭后校验完整库存，才从其字节安装发行 wheels、加载 Runner 和执行 Installer。离线包包含 producer lock 及校验所需的 `deploy/release-producer.Dockerfile`。

## 受限 workload 诊断

Guest observation 之后的流按 D（诊断）和 R（Draft）分别 framing。诊断 schema 为 `animemo.candidate-operation-diagnostic/v1`，绑定 plan/source/tree/Q/verified digest/Profile/session 的 operation digest，每操作最多 16 KiB、40 个事件；Draft 上限 8 MiB。重复、倒序、未知字段/枚举、绑定错误、畸形、截断和超限均拒绝。普通 stdout/stderr 不转为诊断正文。

固定阶段覆盖 SSH 观察、sudo 启动、root 进入、材料完结、runtime 初始化、Runner 启动、平台准备、Installer 执行、Draft 写入/回传；Host 独立记录解析和身份绑定结果。退出码只来自实际子进程，未取得为 null；缺少可信 root 标记时为 UNKNOWN_BEFORE_ROOT_START。标准库启动保护在复杂导入之前发出阶段。内部 Installer 输出有界 drain，stderr 丢弃，超时取消进程组；异常正文、原始流、环境和秘密不进入诊断。

Installer 失败同时记录通用失败和受限的真实 Adapter 错误码，例如
`INSTALL_RUNTIME_START_FAILED`。仅接受 `INSTALLER_FAILURE_CODES` 固定集合；未知异常
或携带任意文本的 code 不能进入诊断。该字段用于区分失败步骤，不推导任何已完成步骤。

普通 Installer/业务失败只有在各层真实退出码及可信阶段足够、canonical continuation 和清理证明安全时，才可继续下一 Profile。共同启动/回执缺陷或交付不确定撤销总会话，其余 Profile 为 NOT_RUN_SHARED_BLOCKER；认证失败、SSH 中断、短写、Guest/源码/材料/lease 漂移不得补发。

诊断不提供成功 authority。仍须 canonical Draft、Host 模板前后态及 Profile Receipt 验证通过；Aggregate v4 / Profile v2 的现行消费者继续拒绝 FAIL、不完整或身份不符。`credential_session` 记录一次 capture attempts/completed、终态和冻结绑定；其 `profiles` 与 `credential_results` 记录每角色 delivery attempts/completed、target/lease 核验及操作结果。capture 不按 Profile 重复计数。初始失败、诊断失败、cleanup 和 POSTSTATE 分别保留。

已获得本固定任务授权时，专用原生 Console 直接运行 `python -B -m scripts.candidate_vm_harness --execute --authorization-id ANIMEMO_V2_CANDIDATE_PR247_REVALIDATION_SINGLE_CAPTURE_V1 --r2-origin-transport cloudflare-plugin`，同时提供准确 Candidate/Q/source 参数及尚不存在的 `--result`。入口接受本进程生成并保存的完整 plan，不授予后续任务或发布权限。Formal 保持其独立调用边界。

#247 后复验的独立固定范围为 `ANIMEMO_V2_CANDIDATE_PR247_REVALIDATION_SINGLE_CAPTURE_V1`，
派生账本是 `E:/1e0c088ec6cb93149000f3d00a88111230dc38a8ebcf8233340e1fcfa5df086f`。
它只提供一次 capture attempt、三个 Profile 各三个固定角色最多九次交付；仍需操作员
明确授权。owner 和 reserve 均要求明确 ID，缺省即拒绝，不回落到旧范围。旧账本保持
原样，改变源码、Q 或 session 不产生新额度。最终源码及未来 Q 在运行时校验并冻结，
不把未来 commit/run ID 写回源码，也不预建账本。未知 ID、通配前缀和后续备用 ID 均拒绝。

开发回归按风险分为 batch 生命周期与额度、Guest authority/真实 Windows holds、受限协议/本机子进程、POSIX root/runtime/receipt、现行 canonical 消费者。测试输入仅 synthetic sentinel；`scripts/tests/native_candidate_console_probe.py` 在独占可见 conhost 自动输入公开文本，验证捕获、退格和取消，不接触真实账本或 Guest。真实发行材料的隔离开发探针须记录源码/材料差异，并明确没有 Candidate authority。

最终三 Profile 验收只使用审查合并后的 exact main、同源 Q 和新 Clone。成功 Q、开发测试或单 Profile PASS 均不能替代三 Profile、Aggregate、Origin 前后态和资源收尾的实际结果。
