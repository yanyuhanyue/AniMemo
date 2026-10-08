# 当前架构

产品边界：核心保存已经发生的观看与个人回忆，不承担追番调度或观看目标。仅作品名、未知日期和自由文字均为有效记录；角色/集数等结构化整理和扩展平台不得成为记录的前置依赖。

后续开发与验证规则以 [当前更新路线](refactor-next-steps.md) 为准，既有实施边界见 [stage-1-3-delivery.md](stage-1-3-delivery.md)，领域职责见 [memory-domain.md](memory-domain.md)。Linux + Docker 是主开发与首批部署基线；Windows 只保留轻量本地接手入口。

## 运行结构

浏览器通过同源 `/api/v1` 访问 Go HTTP 应用，Go 应用使用 PostgreSQL。开发时 Vite 代理 API；容器中由 Go 提供已构建的前端文件。

依赖方向：

```text
cmd/animemo → api → accounts
                 → journal
                 → database

accounts / journal → PostgreSQL
accounts / journal → fault / id
journal → media（图片校验）
```

`accounts` 管理账号、密码散列和服务端会话。`journal` 直接拥有 SQL 与事务，调用者只传入已认证的 owner 和操作数据。`api` 负责 HTTP 解析、认证、错误映射、Cookie 和来源校验。生产代码没有依赖 `tooling` 或 `deploy`。

没有为单一数据库实现额外的通用 Repository / Factory 框架。模块通过公开业务方法测试，数据库场景使用真实 PostgreSQL。

## 数据与并发

- `users`：账号身份和密码散列。
- `sessions`：随机会话令牌的 SHA-256 摘要及过期时间；原始令牌仅交付 Cookie。
- `entries`：独立 UUID、owner、番剧资料、个人状态、标签、短评、进度和版本。
- `watch_records`：观看日期、话数范围、笔记和幂等请求 ID，归属某个 entry。
- `entry_covers`：每个 entry 最多一张原图，保存独立 revision、真实 MIME、像素尺寸、字节数和 `bytea`。外键级联删除，列表只读取 revision，不读取图片二进制。

编辑在事务内锁定 owner 范围内的 entry，验证版本，应用变化，然后更新版本。添加观看记录使用同一条目锁，使请求去重、写入观看记录和推进进度同成同败。删除会级联删除该条目观看记录与封面，并同样要求版本。

导出使用只读 Repeatable Read 事务，确保条目和观看记录来自同一快照。当前是有数量上限的内存导出；出现大规模导出需求后用独立任务流式生成。

## 私有封面

`media` 只负责 Go 标准库图片校验，不依赖数据库、HTTP 或账号。`journal` 拥有封面 SQL、账号范围、容量及版本事务；`api` 负责认证、Origin、原始请求体限制与响应。

- 上传使用 `PUT /api/v1/entries/{id}/cover?version=…` 原始 JPEG / PNG 请求体；删除使用同路径 `DELETE`，均返回更新后的 Entry。
- 下载使用 `GET /api/v1/entries/{id}/cover/{revision}`。每次验证当前会话与 owner，响应为 `private, no-store`、`nosniff` 和 `Cross-Origin-Resource-Policy: same-origin`。旧 revision 返回 404，不能借旧地址读到新图。
- 每张最多 2 MiB、单边不超过 8192、总像素不超过 1200 万。先读尺寸再完整解码，拒绝损坏、格式伪装和超限图片；原始字节及其元数据保持原样。每个 API 实例最多同时处理两个上传，繁忙返回 503 / Retry-After。
- 每账号封面合计不超过 100 MiB。封面修改先获得按 owner 计算的 PostgreSQL 事务 advisory lock，再锁 entry、检查版本及容量，最后写图与递增版本。跨实例并发仍受同一配额约束，替换时扣除原图占用。失败事务保留原图和版本；移除不存在的封面在版本一致时为无操作。

默认采用 PostgreSQL `bytea`；本地提交已增加 R2 后端、暂存上传、媒体引用和清理/迁移流程，由 mediastore 管理。`animemo.journal/v2` JSON 保留记忆和图片引用，但不包含图片二进制；完整图片备份使用 ZIP 或实例快照。

迁移是嵌入 Go 程序的 SQL。启动时在事务与数据库 advisory lock 中执行；已应用 SQL 的校验和发生变化时拒绝继续。新变化通过新增迁移交付。

## 前端

功能目录拥有相应页面、编辑器和数据请求。`openapi-fetch` 使用 OpenAPI 生成类型，TanStack Query 管理服务端状态，组件状态只管理搜索、窗口和表单。

数据查询键包含账号 ID。切换账号先取消在途查询，再删除账号数据缓存，保留会话查询本身并通知订阅者。所有查询传递 AbortSignal，避免上一个账号的迟到响应进入新账号界面。

产品 UI 包装 Base UI 的 Button / Input / Dialog；业务目录不直接依赖底层库。弹窗打开时聚焦输入，支持 Escape 和键盘焦点约束。样式含移动端布局、可见焦点和 reduced-motion 分支。

## 验证与交付

日常验证入口分为前端静态检查、后端静态检查、纯业务测试和 PostgreSQL 集成测试。CI 按受影响目录选择 web / api；未知目录、工具、部署和合同变化检查双方。PR 优先从 base commit 读取选择器，初次引入或无法读取时全部检查。

CI 的固定 `required` 汇总任务核对选中的任务成功、未选中的任务跳过。当前尚未配置远程仓库的分支保护，也未调度 GitHub Actions。

源码检查与部署发行检查分别归属。实例运维、导入 Worker 与插件使用各自的验收入口和测试集合，不把它们加进每次前端修改的必跑路径。

VM 与约 25 分钟持续负载是独立的可选专项，不接入默认开发命令、必需 CI 或发布前置。当前没有这两类自动流程；缺少相关结果应如实标明，而不阻塞不依赖它们的开发。1.1 新增独立标签发行工作流，构建一次并验证同一产物，使用 GitHub 来源证明；普通 CI 不承担发布。流程实现与远端签发验证分别记录，见 [发行说明](release.md)。


## 扩展业务与权限

`accounts` 同时拥有偏好、头像、公开申请、角色、站点设置和 TOTP 生命周期。`governance` 是很小的事务内管理员鉴权 / 审计助手，不引入通用数据访问框架。管理修改先取得固定 advisory lock、重新核对当前角色，再修改目标版本并写审计，保护最后一位可登录管理员。

`journal` 拥有历史修订、批量操作、标签与预设、CSV / JSON / ZIP 校验、持久导入、个人备份、公开投影、专栏及资源回收站。公开 DTO 不带用户邮箱、私人条目 ID、版本和逐次观看历史；私密附件不投影到公开文章。撤回后每次媒体读取仍重新检查权限，API 禁止缓存。

导入任务的原始文件、预览、指纹、状态与结果存 PostgreSQL。worker 用会话 advisory lock 认领任务，进程退出释放锁后其他 worker 可恢复。校验不写手账；确认时锁定条目并核对预览指纹，条目、观看记录、图片与完成回执一次事务提交。大文件有大小与数量上限，ZIP 不解压到磁盘，校验路径、文件类型、总展开大小和 SHA-256 清单。

两步验证使用标准 TOTP，密钥由实例 AES-256-GCM 加密并绑定账号 ID；恢复码只存摘要，事务中单次消费。启用、关闭、重置恢复码均检查当前密码并轮换会话。管理停用与撤销角色后，旧 Cookie 无法继续获得权限。

实例安装 / 备份 / 更新只在 `tooling/instance.mjs`，通过独立 `compose.instance.yaml` 管理专属项目和卷。运行时不接触 Docker。升级先停止 Web 和独立 Worker、持久记录操作阶段、备份并还原为新数据库，旧数据库保留原 schema；失败切回原配置。回滚前另存更新后数据。实例密钥与数据库共同备份，具体操作见 `instance-operations.md`。

`contracts/openapi.json` 仍是唯一合同源，生成 Web 类型和 `apicontract.Document`。浏览器按真实 URL 进入公开页面，其他路径保持登录界面或明确的不存在页；Go 静态回退与 API 兜底同启的情形有回归测试。

## 插件合同与受限执行

`pkg/pluginproto` 定义 Manifest、Request 和 Response；`internal/plugins` 拥有不可变包、启用状态、管理员审计和 wazero WASI 执行。`api` 认证用户、协调文件转换与 `journal.NewImport`，`journal` 不导入插件模块，插件也不能接触 SQL 或事务。

首个消费者是独立编译的 `examples/watch-history-text`。它只得到所选文件名与文本，返回普通导入 JSON；用户核对最多 100 条观看记录明细后确认。插件无文件、网络、环境、持久状态或任意前端脚本能力。生产执行使用独立子进程中的 WASI 沙箱，受时间、输入输出、内存和 Linux CPU / 虚拟地址限制；它仍不等于面向任意恶意发布者的完整 OS 沙箱。

Manifest 协议版本、宿主 API 支持范围和唯一能力均严格校验。同 slug/version 内容不可改写，启停比较 revision 并记录审计。候选升级在迁移前执行只读插件预检查；普通启动将损坏或不兼容的可选包隔离而保留 Core。模块字节与启用状态存在同一数据库快照内，恢复副本后扩展默认停用并要求重新审阅。详细合同、限额和未完成项见 `plugins.md`；进一步的原版能力与旧架构债务应对见 `refactor-next-steps.md`。

## 独立 Worker 与同事务 Outbox

`animemo serve` 不执行后台任务。`animemo worker` 运行导入、邮件、同步、媒体及 Outbox 循环。`memory_revisions` 的新增在业务事务中生成 outbox；租约认领、失效回收、attempt 回执与内部消费去重由 `internal/jobs` 管理。HTTP / 导入 / Outbox 使用 request ID 关联，管理员运行状态展示心跳与积压。

`anime_resources` 和失效外部身份独立于条目，`memory_revisions` 保留有意义变化；详情页最多显示最近 100 条，完整 v2 导出包含全部修订。日期精度与录入时间分离；连载追平不会自动等同作品完结。

## 1.3 记忆库增量

`journal` 新增按职责分开的 notes、characters、episodes、collections、yearly、memory_media、memory_search 和 achievements 文件。API 只负责认证、DTO 与状态码；React 记忆页按用户流程拆开，通过生成的合同访问核心，不导入后端或插件实现。入口与具体容量见 [1.3 交付](v1.3-delivery.md)。

独立笔记可引用持久本地身份，归并通过重定向表达；失效观看引用与删除图片保留正文。记忆串/搜索是即时只读投影，年度修订则是用户明确创建的不可变内容快照，两者不混为另一套可写事实。

成就复用独立 Worker 和 PostgreSQL 待处理标记，不在保存笔记的事务中全量扫描用户。自动投影幂等写入稀疏进度、一次性解锁和独立授予状态；后台补算持久冻结规则及账号集合，最多十个账号一批。没有引入通用规则语言、排名或额外消息服务。

私人图片现阶段直接使用 PostgreSQL bytea 保存原件与缩略图；预留、配额和终结仍由核心控制。这样个人导出和数据库快照都能完整包含图片，不要求先打通 R2 才能使用长期记忆；以后更换存储后端仍需保留同样的原件身份、授权与备份合同。

文件转换宿主现在支持 API 1–2；API 2 增加观看来源字段。随附 TXT 1.1.1 声明只使用 API 2，旧宿主在安装时拒绝，而旧 API 1 转换器仍可在新宿主运行。消息信封 `protocol=1` 未改变；API 版本、产品版本和插件包版本分别管理。
