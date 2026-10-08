# 历史实施记录：阶段 1–3 架构基线与验收

2026-10-07；集成本地提交 `4eb8b88`，继续在 `codex/animemo-next-cloud-check` 工作。这里的三个阶段是“领域合同 → 运行与恢复 → UI / 官方扩展边界”，只记录当时的实施范围。后续版本、顺序和验证规则统一使用 [2026-10-08 新更新路线](refactor-next-steps.md)，本记录不代表旧路线或新版 1.1 的发布要求已完成。保留本地已完成的 Bangumi、邮件、OAuth 同步和 R2 实现，不为旧项目的数据和插件做兼容层。

## 阶段 1：领域边界

已落地：

- `anime_resources` 使用独立本地 UUID，`entries.anime_id` 关联作品身份。外部 Bangumi ID 只是映射；解绑后保留失效的映射和历史，不把外部 ID 当个人记忆主键。
- 当前条目是可编辑投影；评分、状态、笔记、进度及观看更正形成 `memory_revisions`。业务写入和修订同事务提交；失败事务不留下假历史。移出清单后私有历史仍可完整导出；删除账号才移除该账号全部数据。
- `day / month / year / unknown` 是观看事实的一部分。未知日期存 NULL，月份和年份的内部补位不表示具体某一天。统计只把精确日算作观看日，月度分布只包括日/月精度；全部记录数仍含未知时间。
- `airing_state` 与个人观看状态分开。连载中或不确定的作品达到当前总话数，自动置为 `caught_up`；明确完结才自动置为 `completed`。手动编辑仍允许用户明确表达完成状态。UI 新建默认不确定；API 省略播出状态时沿用有限剧集的 `finished` 默认值，API 客户端应显式填写。
- JSON/ZIP 导出升级为 `animemo.journal/v2`，携带作品身份、失效映射、观看精度及全部修订，包括已经移出清单的记忆。完整包只能导入空手账，避免“同名跳过”丢掉记忆；恢复重新分配目标账号的 UUID，保留修订发生时间。CSV/TXT 仍走普通导入预览；`v1` 是当前 TXT 消费者使用的受限导入 DTO，不是旧项目兼容承诺。

冻结的后续领域合同见 [memory-domain.md](memory-domain.md)。角色、正式 Episode 目录、独立长笔记 / Anchor、收藏与排行榜等尚无产品实现；不能把领域命名当作这些功能已经完成。当前观看进度仍是“最远一话”的断言，不代表逐话完整覆盖。

## 阶段 2：运行与恢复

- Web 只接收请求。导入、邮件、外部同步、媒体清理和 Outbox 在独立 `animemo worker` 进程运行；Docker Web / Worker 是两个容器。`npm run dev` 同时管理三个进程，`npm run containers` 启用 Worker profile。
- 记忆修订在同一事务写入 `core_outbox`。认领有 lease token、过期回收、attempt 回执和有限退避；消费端以 revision ID 去重，与确认完成同事务提交。过期执行者不能覆盖新 lease 的结果。
- HTTP 生成 request ID，导入与 Outbox 可关联到原请求；结构化任务日志和管理员运行状态提供 Worker 心跳、积压、执行与失败数量。这是最低限度观测，不等于完整 OpenTelemetry 平台。已有邮件、同步、媒体任务保留各自业务回执，不强行重写成通用任务框架。
- 更新操作将 prepared / stopped / staging / switching / completed 等阶段持久化到私有 `operation.json`；中断后 `instance recover` 可从已记录状态恢复，切换后的新写入先存 rescue 备份。旧数据库和候选数据库保留。该日志包含实例配置，不得提交或放入诊断包。
- 备份/切换停止 Web 和 Worker；恢复副本撤销会话、邮件令牌和外部连接，并默认停用扩展，要求重新审阅。Linux 媒体恢复使用备份文件所属 UID/GID 的一次性容器读取 0600 文件，保留只读根文件系统、无 capabilities 和 no-new-privileges；不放宽备份权限，不改变 Web/Worker 非 root 运行方式。

限制：目前只有一个本地运维进程负责实例更新；不是多主控制面。更新已有持久阶段恢复，原有显式 rollback 有 rescue 快照，但尚未统一成同样的持久状态机。Outbox 当前唯一消费者是内部记忆活动投影，尚未开放 Bridge 或插件事件订阅。

## 阶段 3：UI 与第一方扩展

- React 业务目录只使用 `components/ui` 的 Button / Input / Dialog；Base UI 依赖收在组件层，布局检查阻止业务直接依赖底层 UI / 动效库。Tailwind 用于受控样式工具，产品语义 token 和二次元视觉皮肤独立于领域逻辑。
- 原创暮色车站人物插画、樱粉 / 淡紫配色、柔和手账卡片，覆盖登录、手账、表单和管理界面。图片为本地静态资源，不依赖外部 CDN；保留键盘操作、焦点、reduced-motion 和手机布局。
- 2026-10-08 后台改为独立管理布局：概览、用户、内容、标签、扩展、站点、健康和审计共八页；侧栏与 hash 导航支持刷新、后退。管理表格、确认弹窗和分区卡片共享后台样式，保持领域与 API 边界不变。覆盖四种屏宽、真实保存和扩展启停；详见最新验收记录。
- 官方 TXT 扩展随发行镜像打包。`bundled-extensions.json` 绑定准确的 Core 二进制摘要和插件包摘要；只有这个受控入口能赋予 `ANIMEMO_FIRST_PARTY`，上传清单不能自封官方。
- 生产转换在独立子进程执行 WASI；无数据库凭据、宿主环境、网络或文件挂载传给模块。线性内存、输入、输出、时间和并发有上限；Linux 额外限制 CPU 时间与虚拟地址空间。安装校验仍在宿主进程，不把本阶段宣称为可运行任意恶意插件的完整 OS 沙箱。
- 记录安装 ID、包身份、调用者和执行回执；本地扩展可卸载，核心记忆不随包删除。随附包属于镜像基线，可停用。包损坏会隔离；普通启动仍保留主站，候选升级继续严格预检，激活前再次验证摘要。
- 恢复后扩展默认停用。官方身份不意味着自动授予已有手账读取、网络、后台写入或任意前端执行权限；当前能力仍只有用户主动选择的文件转换。

尚未实现：第三方发布者认证与撤销链、市场、个人持久授权中心、Extension Bundle 外部状态、Bridge、插件后台任务 / UI 容器。既没有假装这些已完成，也没有把缺口归因于网络。

## 对应实现的验收入口

下列命令用于相应范围的验证，不要求每次修改顺序运行整套；按当前路线选择适用入口。

```sh
npm run verify
ANIMEMO_PREVIOUS_IMAGE=animemo-next-app:latest ANIMEMO_CANDIDATE_IMAGE=animemo-next-stage3:20261007 npm run test:instance
ANIMEMO_CANDIDATE_IMAGE=animemo-next-stage3:20261007 npm run test:stage3
node tooling/stage3-browser.mjs
```

浏览器脚本读取 `.local/output/stage3-review-access.json`（或 `ANIMEMO_REVIEW_ACCESS` 指定文件）的 `origin/email/password`，只使用合成账号；Playwright 和浏览器路径可通过 `ANIMEMO_PLAYWRIGHT` / `ANIMEMO_BROWSER` 指定。脚本会创建合成条目，不能拿真实个人账号直接跑。

最新结果和证据见 [verification.md](verification.md)。云端验收实例是 `stage3-review`，使用独立数据库与卷；云端回环地址为 `http://127.0.0.1:18082`，它不等于用户 Windows 上的同名地址。当前环境没有公网预览或 VPN 能力，用户侧访问需要已有端口转发，或按 [cloud-development.md](cloud-development.md) 在本地启动。应用已启动与公网可访问是两件事。

Bangumi / OAuth、Resend 和 R2 的真实外部联调仍需网络与凭据，沿用 [local-network-handoff.md](local-network-handoff.md)。本轮不触发 GitHub CI，也不以受控协议测试代替真实服务交付。
