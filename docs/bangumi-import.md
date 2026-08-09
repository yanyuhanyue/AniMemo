# Bangumi 收藏导入

Phase C 提供用户主动触发的只读收藏发现与显式导入。连接 Bangumi 后不会自动创建任何手账，也不会向 Bangumi 执行收藏、评分、评论或章节进度写操作。

## 流程

1. 用户点击“读取 Bangumi 收藏”。
2. 后端解密服务端保存的凭据，并分页读取 `GET /v0/users/{username}/collections`。
3. 请求固定 `subject_type=2`、每页最多 50 条，只保留 Anime；总量受 `BANGUMI_IMPORT_MAX_ITEMS` 限制。
4. 后端生成带 TTL 的用户私有 snapshot，并返回分页 Preview。
5. 用户逐项选择 `CREATE_NEW`、`BIND_EXISTING`、`IMPORT_SAFE_USER_FIELDS` 或 `SKIP`。
6. 后端只信任 snapshot 中的外部身份和收藏字段，再显式 Apply。

`GET /v0/users/{username}/collections/{subject_id}` 的当前官方 schema 也已核对，可供单项只读发现使用。Bangumi 收藏状态集中映射为：想看 -> `planned`、在看 -> `watching`、看过 -> `completed`、搁置/抛弃 -> `on_hold`。评分 1 至 10 映射为 `personal_score`；0 或缺失映射为 `None`。用户收藏 tags 仅保存在 snapshot/provenance，不覆盖 AniMemo tags。

## 匹配规则

- 已有相同 `provider + external_id`：权威匹配到已绑定手账。
- 仅标题相同：只标记 `possible_local_match`，绝不自动绑定。
- 没有身份匹配：用户可创建新手账，或明确选择一个未绑定的本地手账。

创建与绑定前，服务端通过公共 subject API 获取权威作品资料；客户端提交的标题、评分、评论或 URL 不能替换 snapshot。新建手账与 `ExternalMediaIdentity` 在同一事务中完成。绑定和导入使用既有用户行锁语义，PostgreSQL 下同时导入同一用户的同一 Bangumi subject 最终只能产生一个身份。

## 冲突策略

默认策略为 **LOCAL WINS**：对已存在的手账，远端 `personal_score`、`watch_status`、`review` 均不自动覆盖。用户可以在 Preview 中逐字段明确选择远端值；未勾选字段保持本地值。`tags` 在 Phase C 不提供覆盖选项。

新建手账没有本地冲突，因此可以用 snapshot 中的收藏状态、有效评分和短评初始化。逐项执行使用 savepoint：一个项目格式错误、Provider 资料失败或绑定冲突，只产生该项失败结果，不回滚同批其他成功项目。

Apply 对 preview 行加锁并保存结果；首次完成后清空远端 snapshot，只保留最小结果摘要。重复 Apply 返回相同结果，不重复创建数据。过期、已消费或跨用户的 preview 都不会披露内容。

## 阶段限制

Bangumi OpenAPI 明确提示 collection `updated_at` 不可靠。本阶段只把它作为最小 provenance 保存，不用它进行同步判断。Phase D 才会设计 outbound status、rating、comment、episode progress、字段所有权、per-field sync state、冲突状态与人工解决；不会从 Phase C 自动开启后台轮询、调度器或双向同步。
