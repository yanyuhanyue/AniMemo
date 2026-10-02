# 单 Runtime DEV 的条件执行与留存

本入口只服务 `LOCAL_INSTALLER_DEVELOPMENT` 的显式单
`RUNTIME_BASE_OFFLINE` 计划。默认三 Profile、Candidate 和 Formal 继续使用
原来的材料、信任和清理合同。准备或本地 fixture 验证不会产生真实安装授权。

## 一条调用链

1. `local_candidate_development` 固定执行源码、原产品身份、独立 Linux DEV
   信任选择、绝对截止和 `STOP_AND_RETAIN`。原生批次确认冻结资源范围。
2. Provider 在一次启动调用之前记录宿主墙钟与单调时钟。启动后的所有阶段共享
   最多四小时的总预算；不能在 Guest READY、root 进入或重连时重新计时。
3. `candidate_guest_session.bootstrap_candidate` 先核验连接，再运行固定、
   有限、无提权的 Runtime 基线。工具、包、架构或本地 socket 的硬缺项拒绝；
   只有权限相关事实可保留 UNKNOWN。结果绑定源码、材料、clone、boot 和 session。
4. `runtime_development_boundary` 以该观察构造准确操作边界，再执行无密码的
   有界原生确认。重新检查身份、新鲜度和期限后，才进入现有一次密码捕获。
5. 仍只使用 `BOOTSTRAP_ROTATION`、`VERIFIED_SUDO`、`CANDIDATE_WORKLOAD`
   三个角色。最后一个角色传送固定源码、原候选材料和单独的 Runtime DEV 输入。
6. 固定 root 程序先检查准备写入范围，再封闭材料、建立本次根入口交接记录。
   真实 UID/EUID、boot、机器、工具和本地 daemon 观察须与边界相符。
7. `development_trust` 在 Linux 上重新验证两域 retained TUF 和原 portable /
   sidecar，调用真正的 `LocalBundleReleaseSource.from_media`。普通选择 JSON
   和 TEST_ONLY 结果都不是 `DevelopmentLocalBundleAuthority`。
8. 真正的平台及 Installer planner 输出完整计划。`DevelopmentExecutionGate`
   导出规范表达、比较准备/业务/资源/停止范围，并签发进程内单次消费状态。
   较低层 `Installer.execute` 同样要求该条件门；`--accept` 无法替代它。
9. 在同一个 root 工作负载结束前，关闭本实例 updater、按精确 ID 停止已核验
   ownership 的容器并回读，再关闭 listener。保留应用数据，不删除卷或全局 prune。
10. Provider 软停新 clone 并回读，清除会话密钥、连接、租约和材料持有。
    `RETAINED_STOPPED` 需要准确业务停止和副本身份回执；owner 不把留存谎报成删除。

## 信任与来源

原 rc.3 签名只证明原 portable 和镜像字节。当前未发布 DEV 代码的覆盖范围来自
独立本地批准和准确执行清单；不会借用原 Q 签名来证明新源码。Runtime trust 输入
独立暂存，不覆盖执行树中的原 Q producer extras。

Windows verifier 的真实密码学结果仍是 Windows 观察。Linux verifier 的构建
出处、ELF 平台和摘要必须单独闭合。两域非回退下界为 GitHub `9/974/79/10`、
Sigstore `15/795/165/14`，每次实际消费仍检查当前时间和签名。

该受限安装路径不把 DEV trust provision 成生产 trust。初始 adoption 使用同一
Installer 本地来源；本轮结束前停止 updater。以后真正的离线更新需要它自己的
生产信任和操作授权，不能从这次 DEV 留存结果推出。

## 期限与失败

以启动调用前的记录作为保守 boot 起点。各层选取绝对授权截止、boot 四小时、
owner/租约及阶段限制中更短的边界，并预留停止时间。跨机器只传绑定的 UTC
截止和剩余秒数；各自使用本机单调时钟。墙钟回退、身份改变、缺字段、重复
消费、越界动作或结束未知均拒绝业务执行。截止后的必要清理单独记录超界事实。

停止失败不能由 VM 已关闭替代；报告写入失败也不能变成安装成功。主失败保留，
清理失败作为额外原因记录。旧 Candidate/Formal 成功后的删除策略保持原合同。

## 本地验收的含义

本地检查应覆盖真实构造器生成的 Console/Guest/root/runner 程序、compile、
编码运输往返和 Windows UTF-16/NUL 上限；不更改宿主全局编码。真实 planner、
规范序列化、条件门和结果消费者使用明确 OS/资源 fixture 与效果记录器。
无秘密真实子进程测试使用逐测试精确命令白名单、违规锁存和 Job 子树回收回执。
密码学验证使用真实保留的签名材料，不 Mock 验签成功。

这些结果属于本地接线与材料集成验证。Linux 实跑、Guest 基线、现场 root plan、
安装、D2、Candidate、Formal 和发行均需分别取得并记录实际证据。修改后的源码
不能自动继承尚未消费的旧真实执行授权。
