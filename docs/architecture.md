# 当前架构

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

当前采用独立表中的 PostgreSQL `bytea`，与条目元数据同事务提交，直接复用数据库卷和备份机制。数量受限的私有封面无需增加文件存储服务或异步清理任务；未来确有大规模媒体需求时再迁移存储。`animemo.journal/v1` JSON 导出仅增加可空的 `cover_revision` 元数据，不包含二进制，也不能作为图片备份；界面会提示这一范围。

迁移是嵌入 Go 程序的 SQL。启动时在事务与数据库 advisory lock 中执行；已应用 SQL 的校验和发生变化时拒绝继续。新变化通过新增迁移交付。

## 前端

功能目录拥有相应页面、编辑器和数据请求。`openapi-fetch` 使用 OpenAPI 生成类型，TanStack Query 管理服务端状态，组件状态只管理搜索、窗口和表单。

数据查询键包含账号 ID。切换账号先取消在途查询，再删除账号数据缓存，保留会话查询本身并通知订阅者。所有查询传递 AbortSignal，避免上一个账号的迟到响应进入新账号界面。

弹窗使用原生 dialog；打开时聚焦输入，支持 Escape 和键盘焦点约束。样式含移动端布局、可见焦点和 reduced-motion 分支。

## 验证与交付

日常验证入口分为前端静态检查、后端静态检查、纯业务测试和 PostgreSQL 集成测试。CI 按受影响目录选择 web / api；未知目录、工具、部署和合同变化检查双方。PR 优先从 base commit 读取选择器，初次引入或无法读取时全部检查。

CI 的固定 `required` 汇总任务核对选中的任务成功、未选中的任务跳过。当前尚未配置远程仓库的分支保护，也未调度 GitHub Actions。

源码检查与部署发行检查分别归属。实例运维、导入 Worker 与插件使用各自的验收入口和测试集合，不把它们加进每次前端修改的必跑路径。


## 扩展业务与权限

`accounts` 同时拥有偏好、头像、公开申请、角色、站点设置和 TOTP 生命周期。`governance` 是很小的事务内管理员鉴权 / 审计助手，不引入通用数据访问框架。管理修改先取得固定 advisory lock、重新核对当前角色，再修改目标版本并写审计，保护最后一位可登录管理员。

`journal` 拥有历史修订、批量操作、标签与预设、CSV / JSON / ZIP 校验、持久导入、个人备份、公开投影、专栏及资源回收站。公开 DTO 不带用户邮箱、私人条目 ID、版本和逐次观看历史；私密附件不投影到公开文章。撤回后每次媒体读取仍重新检查权限，API 禁止缓存。

导入任务的原始文件、预览、指纹、状态与结果存 PostgreSQL。worker 用会话 advisory lock 认领任务，进程退出释放锁后其他 worker 可恢复。校验不写手账；确认时锁定条目并核对预览指纹，条目、观看记录、图片与完成回执一次事务提交。大文件有大小与数量上限，ZIP 不解压到磁盘，校验路径、文件类型、总展开大小和 SHA-256 清单。

两步验证使用标准 TOTP，密钥由实例 AES-256-GCM 加密并绑定账号 ID；恢复码只存摘要，事务中单次消费。启用、关闭、重置恢复码均检查当前密码并轮换会话。管理停用与撤销角色后，旧 Cookie 无法继续获得权限。

实例安装 / 备份 / 更新只在 `tooling/instance.mjs`，通过独立 `compose.instance.yaml` 管理专属项目和卷。运行时不接触 Docker。升级先停止应用、备份并还原为新数据库，旧数据库保留原 schema；失败切回原配置。回滚前另存更新后数据。实例密钥与数据库共同备份，具体操作见 `instance-operations.md`。

`contracts/openapi.json` 仍是唯一合同源，生成 Web 类型和 `apicontract.Document`。浏览器按真实 URL 进入公开页面，其他路径保持登录界面或明确的不存在页；Go 静态回退与 API 兜底同启的情形有回归测试。

## 插件合同与受限执行

`pkg/pluginproto` 定义 Manifest、Request 和 Response；`internal/plugins` 拥有不可变包、启用状态、管理员审计和 wazero WASI 执行。`api` 认证用户、协调文件转换与 `journal.NewImport`，`journal` 不导入插件模块，插件也不能接触 SQL 或事务。

首个消费者是独立编译的 `examples/watch-history-text`。它只得到所选文件名与文本，返回普通导入 JSON；用户核对最多 100 条观看记录明细后确认。插件无文件、网络、环境、持久状态或任意前端脚本能力。当前是嵌入式 WebAssembly 沙箱，绝不等同独立 OS 进程隔离。

Manifest 协议版本、宿主 API 支持范围和唯一能力均严格校验。同 slug/version 内容不可改写，启停比较 revision 并记录审计。启动和候选镜像在数据库迁移前执行只读插件预检查；模块字节与启用状态存在同一数据库快照内，因此本阶段的备份 / 回滚可同时恢复它们。详细合同、限额和未完成项见 `plugins.md`；进一步的原版能力与旧架构债务应对见 `refactor-next-steps.md`。
