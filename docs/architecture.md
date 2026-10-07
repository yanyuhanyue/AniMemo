# 第一阶段架构

## 运行结构

浏览器通过同源 `/api/v1` 访问 Go HTTP 应用，Go 应用使用 PostgreSQL。开发时 Vite 代理 API；容器中由 Go 提供已构建的前端文件。

依赖方向：

```text
cmd/animemo → api → accounts
                 → journal
                 → database

accounts / journal → PostgreSQL
accounts / journal → fault / id
```

`accounts` 管理账号、密码散列和服务端会话。`journal` 直接拥有 SQL 与事务，调用者只传入已认证的 owner 和操作数据。`api` 负责 HTTP 解析、认证、错误映射、Cookie 和来源校验。生产代码没有依赖 `tooling` 或 `deploy`。

没有为单一数据库实现额外的通用 Repository / Factory 框架。模块通过公开业务方法测试，数据库场景使用真实 PostgreSQL。

## 数据与并发

- `users`：账号身份和密码散列。
- `sessions`：随机会话令牌的 SHA-256 摘要及过期时间；原始令牌仅交付 Cookie。
- `entries`：独立 UUID、owner、番剧资料、个人状态、标签、短评、进度和版本。
- `watch_records`：观看日期、话数范围、笔记和幂等请求 ID，归属某个 entry。

编辑在事务内锁定 owner 范围内的 entry，验证版本，应用变化，然后更新版本。添加观看记录使用同一条目锁，使请求去重、写入观看记录和推进进度同成同败。删除会级联删除该条目观看记录，并同样要求版本。

导出使用只读 Repeatable Read 事务，确保条目和观看记录来自同一快照。当前是有数量上限的内存导出；出现大规模导出需求后用独立任务流式生成。

迁移是嵌入 Go 程序的 SQL。启动时在事务与数据库 advisory lock 中执行；已应用 SQL 的校验和发生变化时拒绝继续。新变化通过新增迁移交付。

## 前端

功能目录拥有相应页面、编辑器和数据请求。`openapi-fetch` 使用 OpenAPI 生成类型，TanStack Query 管理服务端状态，组件状态只管理搜索、窗口和表单。

数据查询键包含账号 ID。切换账号先取消在途查询，再删除账号数据缓存，保留会话查询本身并通知订阅者。所有查询传递 AbortSignal，避免上一个账号的迟到响应进入新账号界面。

弹窗使用原生 dialog；打开时聚焦输入，支持 Escape 和键盘焦点约束。样式含移动端布局、可见焦点和 reduced-motion 分支。

## 验证与交付

日常验证入口分为前端静态检查、后端静态检查、纯业务测试和 PostgreSQL 集成测试。CI 按受影响目录选择 web / api；未知目录、工具、部署和合同变化检查双方。PR 优先从 base commit 读取选择器，初次引入或无法读取时全部检查。

CI 的固定 `required` 汇总任务核对选中的任务成功、未选中的任务跳过。当前尚未配置远程仓库的分支保护，也未调度 GitHub Actions。

源码检查与部署发行检查分别归属。后续增加安装器、Worker 或插件时，需要自己的入口和测试集合，不把它们加进每次前端修改的必跑路径。
