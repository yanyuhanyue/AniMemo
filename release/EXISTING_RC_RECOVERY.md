# 原 v2.0.0-rc.1 的一次受控恢复

此入口只完成 `rc-recovery-policy.json` 固定的既有事务，不赋予任意旧
Candidate 跨源码发布权。产品为 M1 `715e997e1b8ef552376148fee62b08d7c0c35364`，
工具为 PR #255 受保护 squash 合并后实际 main 的 R；两者分别验证。

## 授权与领取

本轮用户已批准 `ANIMEMO_V2_EXISTING_RC_SOURCE_BOUND_RECOVERY_V1`，原方案
第 6 节明确从本次确认起 24 小时。确认记录 UTC 为
`2026-09-14T07:12:07.159Z`，截止为 `2026-09-15T07:12:07.159Z`。
代码准备、检查或 dispatch 不能重新开始该窗口。`runCreatedAt` 单独记录
GitHub 实际运行创建时间，用于唯一 run / nonce 绑定。

`release-recovery.yml` 默认 inspect。execute 只接受受保护 main、原仓库
owner 的真实 workflow_dispatch、PR #255 已合并源码与 reviewed tree 一致、
直接父提交 M1、完整运行历史中的第一次 execute、attempt 1。运行标题仅作
历史索引，还必须核对 API 的 repository/operator/workflow/ref/head/run。
JSON 摘要只提供绑定，不能单独授予权限；没有自签密钥或 allow-mismatch。

原事务必须存在于固定 ref/head、revision 30。首次 CAS 仅追加领取记录，
不得同时修改 step。claim 以后不可移除或替换，旧 attempts 保持原字节。
后续只接受本运行确认写入的连续 head；普通发布入口拒绝已领取事务。
失败运行即消耗机会，禁止 rerun 或第二次 dispatch。

## 剩余操作与时效

先使用原 Q 三份 API 绑定 ZIP、原 C1 canonical 字节和原 plan 验证材料。
Portable 仅由原 OCI 做确定性运输封装，必须与原大小和摘要完全一致。
不重建 runtime、不重新签发五个旧 Attestation。

独立双快照预检从开始观察起最多 900 秒，覆盖原主体、原件、Tag、Draft、
四 registry 键、五证明、设置、工作区与竞争 Actions。每次写前另存当前
journal head/revision/identity 与有效预检的关联观察。同一运行可只读刷新，
固定 24 小时期限不变。POST/PATCH 和 git push 的最终发送前还有无网络
时效检查；准备耗时导致过期时停止，不能递归刷新后悄悄重放请求。

唯一 Draft/Release ID 是 `388147631`。认证发现完整分页后按 ID 回读，
已有草稿 SAME 才提交该 step。缺失资产依原 5 个 step 顺序上传，每个最多
一次请求；固定上传主机和 ID 路径，无重定向、覆盖、删除或 Draft POST。
每个同名资产须状态、大小、摘要及认证下载字节一致。丢失响应只读调和；
结果不明即停止。五资产提交后才发布该 ID，`make_latest=false`。

## 证明和消费者

真正发布以后验证五份匿名资产和 GitHub immutable Release 证明。原五份
Attestation 的 signer 始终是原 `release.yml` / M1。七份证明侧车与原
13 份 metadata 组成独立 Actions Artifact；恢复 claim、追加进度、实际
写请求和 R/run 放在另一执行证据 Artifact。绝不增加第六份 Release 资产。
执行记录不能宣称自身运行已成功，最终结论必须由平台 API 回读。

只读消费者核对实际 run / merged PR / reviewed tree、原 31 份 journal
到最终 COMPLETE 的完整追加链，以及 Artifact ID 的 ZIP 摘要和封闭文件
集合。公开 Release ID、annotated Tag object、侧车字节也与恢复记录绑定。
产品身份仍是 M1。恢复分支的 canonical Mirror 只接受
`workflow_dispatch@main`、run/artifact head R；不把旧 tag 的 release 事件
当作执行 R 的证明。发布后先查实际 Mirror 状态，确认没有自动/未知事务
才使用本轮获准的一次手动 Mirror。其权限仍由现有 Mirror workflow 管理。

## 本地验证入口

运行 `python -m unittest scripts.tests.test_source_bound_recovery
scripts.tests.test_source_bound_recovery_replay
scripts.tests.test_source_bound_recovery_consumer`。

fixtures 保存原 31 份 journal 和 notes，用隔离 local journal/fake transport
覆盖生产 runtime/controller/guard 的续接和失败路径。完整原字节回放可设置
`ANIMEMO_RECOVERY_REPLAY_REAL_BYTES=1`，读取本工作区本任务固定目录的实际
`material-replay-final-result.json`。环境变量只控制是否运行，不提供文件路径，
生产入口不接受它。测试输出均非发行权限或正式验收。

出现真实写入不确定、确定性实现错误或过期时，保全 journal 与远端对象；
不得边改代码边续用许可。新的运行需要绑定最新真实状态的独立窄授权。
本入口没有新 Q/Candidate/Guest/sudo/Formal/Stable/生产权限。
