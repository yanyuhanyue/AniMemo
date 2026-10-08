# AniMemo

使用 Go、TypeScript 和 PostgreSQL 构建的个人番剧记录与回忆档案。用于记下看过的作品和相关经历，不承担追番排期、更新提醒或观看任务。按全新项目开发，不要求兼容旧版数据和接口。

当前已实现个人手账、观看与重看记录、资料与小数评分、批量管理、标签颜色和筛选、账号偏好与两步验证、持久导入任务、含图片备份、公开分享、专栏及审核、管理后台和独立实例运维。新增受限插件文件转换、TXT 观看记录导入及管理员本地包管理。原项目对照、验收口径及未完成项见 [功能清单](docs/feature-parity.md)，不要把这份功能概览当作所有外部集成均已完成。

后续开发以 [当前更新路线：Linux 开发与渐进交付](docs/refactor-next-steps.md) 为准，旧更新路线已被替代。当前已推进领域记忆、独立 Worker/Outbox、更新恢复和 UI/官方扩展边界；实施记录见 [阶段 1–3 交付](docs/stage-1-3-delivery.md)。界面采用原创二次元插画和独立设计 token。

当前源码为 **1.3.0-rc.4**：新建只需作品名；看过但记不清细节是有效记录，感想、日期和集数按需补充。已增加独立记忆库、角色/长篇目录、私人图片、收藏检索、冻结年度册、成就及精细 TXT 追加导入，见 [1.3 交付](docs/v1.3-delivery.md)。成就支持自定义图片、等级边框与已有记录自动授予，见 [成就内容](docs/achievement-content.md)。实例运维包含诊断、配置预览/失败恢复、v3 备份清单与恢复计划、age 加密转移和最小可信发行入口，见 [实例操作](docs/instance-operations.md) 与 [发行说明](docs/release.md)。开发候选尚未完成正式签发/Release 发布。

源码沿用 [PolyForm Noncommercial 1.0.0](LICENSE)，适用范围见 [NOTICE](NOTICE)。

本地接手已补齐 Bangumi 资料搜索 / 绑定 / 刷新、邮件验证与找回、OAuth 收藏同步、R2 图片迁移及媒体备份。Bangumi 公共 API 已真实验证；邮件、OAuth、R2 已通过受控协议和页面测试，正式联调还缺服务凭据。配置与验证边界见 [外部服务接入](docs/local-network-handoff.md)。

## 开发环境

主开发与首批部署基线为 Linux + Docker Engine，首批目标为 Linux amd64 + PostgreSQL 17；具体已验证环境见验收记录。开发需要 Node.js 24.12+、Go 1.26.6+ 和 npm；容器验收需要 Docker Compose 2.24.4+。

```sh
. ./tooling/env.sh
npm ci --ignore-scripts
npm run db:start
npm run dev
```

- Vite 页面：`http://127.0.0.1:5177`
- Go API：`http://127.0.0.1:18081`
- 开发 PostgreSQL：`127.0.0.1:55432`

`Ctrl+C` 停止 Web、Worker 和 Vite；`npm run db:stop` 停止开发数据库并保留数据。`db:start` 自动生成随机开发数据库密码，保存在忽略的 `.local/config.json`，数据在 `.local/data/compose-postgres`。使用 `DATABASE_URL` 时请指向专用开发数据库；应用启动会应用自身迁移。

工具自动识别 `.local/tools/go/bin/go`（Windows 为 `go.exe`），也可设置 `ANIMEMO_GO`。Windows 保留本地开发、用户验收与外部服务联调入口，不代表承诺 Windows 原生生产部署。PowerShell 先执行 `. ./tooling/env.ps1`；推荐 Docker Desktop 的 Linux 容器或 WSL2，原生 PostgreSQL 的路径限制不进入云端默认流程。

## 容器与首次管理初始化

```sh
npm run containers
```

开发容器页面和 API 同源为 `http://127.0.0.1:18081`。默认仅绑定回环地址；设置 `PUBLIC_ORIGIN` 时使用准确的 HTTPS 来源，不带路径。非回环 HTTP 会被拒绝。应用使用非 root 用户、只读根文件系统和无额外 capabilities。

开发脚本自动生成并持久保留 `.local/config.json` 中的 `setupToken` 和 `secretKey`，分别用于首次管理员初始化和两步验证密钥加密。在本地查看初始化口令，打开 `/setup` 创建管理员；不要把配置内容发到聊天或提交 Git。普通注册不会自动获得管理员权限。正式或多个独立实例使用以下运维入口：

```sh
npm run instance -- install --name home --image animemo-next-app --port 18082
npm run instance -- setup-code --name home
npm run instance -- status --name home
```

备份、迁移、更新和回滚的完整步骤见 [实例运维](docs/instance-operations.md)。镜像使用本地不可变 ID，更新在数据库副本上执行迁移，失败自动恢复旧应用；显式回滚前保存更新后数据的 rescue 备份。

## 直接在开发中验收

```sh
npm run check:web        # 合同生成一致性 + TypeScript
npm run check:api        # Go vet + 模块依赖检查
npm test                # Go 单元与 Node 回归
npm run test:api         # 真实 PostgreSQL 集成，使用隔离 schema
npm run verify          # 静态检查 + Node + Go 单元/集成 -race
npm run test:containers # 隔离镜像/容器业务、持久任务与重建留存
npm run test:instance   # 隔离实例安装、备份还原、更新回滚与故障恢复
npm run test:stage3     # 指定 ANIMEMO_CANDIDATE_IMAGE，验证运行与扩展恢复
npm run build           # Web 与 Go 构建产物
npm run plugin:example  # 构建 TXT 示例 WASI 插件包
npm run test:plugins    # 真实沙箱、权限、插件导入与兼容回归
```

日常按 [验证规则](docs/refactor-next-steps.md#3-验证规则与时间成本) 选择受影响检查；集成或部署改动补真实容器验收，不必等提交后才在 GitHub CI 定位问题。`verify` 是主动选择的全量入口，不是每次修改/本地提交的固定前置；它已合并 Go 单元和集成测试，通过后无需重复跑同一范围。Go race 需要兼容的 C 编译器。可传入目标用例，例如 `npm run test:api -- -run TestPublicSharing -race`。

VM 验收和约 25 分钟持续负载测试均为可选手动专项，不进入默认命令、PR 必需检查或发布前置依赖。未执行不阻塞开发，也不记为通过；只有具体问题需要时才单独安排。当前仓库没有强制 VM 或长时压测流程。

数据库测试创建随机 `test_*` schema 并清理；容器和实例验收用独立随机项目、端口和数据卷，结束时清理自己的资源。实际结果见 [验收记录](docs/verification.md)。没有执行远程 GitHub CI 的情况下，不把本地结果表述为远程通过。

修改 API 后执行 `npm run contracts`，同时生成 Web 类型和服务端内嵌合同。运行中的合同为 `/api/v1/openapi.json`；认证、错误、分页、导入和公开规则见 [API 约定](docs/api.md)。

## 产品规则

- 新条目默认私密。可改为持链接可见或公开，并在账号设置显式启用分享。公开手账需管理员审核；关闭分享会使条目链接及媒体读取立即失效。私人观看日期和逐次观看笔记不进入公开投影。
- 修改和删除携带版本；并发修改冲突返回 409。批量状态、标签、可见性变更整体提交，任一条目失败则整体回滚。
- 观看 POST 使用 request_id 防重复；支持重看、记录修改 / 删除、日期筛选和分页。进度按最远已记录话数计算，修改历史后重新计算。日期保留用户的日历日期，时间戳为 UTC。
- 手账封面每张最多 2 MiB，最长边 8192、总像素 1200 万，每账号合计最多 100 MiB；专栏封面另限 20 MiB，头像最多 2 MiB。原图保存在 PostgreSQL，可能包含上传文件自带的元数据。旧图片修订地址失效，公开读取同样检查当前权限。
- 导入先校验预览，确认后原子提交；普通 CSV/TXT 转换按规范化名称跳过重复，不覆盖旧条目。完整 `animemo.journal/v3` JSON/ZIP 仅恢复到空手账，保留身份、日期精度、历史修订和记忆库；仍支持本项目 v1/v2 文件。任务可跨刷新和进程重启恢复，预览 24 小时有效，导入结果统一私密。
- v3 JSON 包含记忆库私人图片及成就历史图案；个人 ZIP 另外包含番剧封面。个人包不包含账号设置、专栏或实例凭据，整个实例备份使用独立运维命令。无新增记忆库内容时可输出 v2，具体范围见 [1.3 交付](docs/v1.3-delivery.md#txt-与完整备份)。
- 专栏以纯文本保留分段，支持关联番剧和封面。修改公开文章会变为草稿，投稿后审核公开；撤回立即停止公开。管理员回收站恢复的条目先变私密，文章先变草稿。
- 启用两步验证后，登录需要 TOTP 或一次性恢复码。密码、两步验证或角色变更会撤销相关会话。实例加密密钥必须与数据库一起备份。

## 文件与环境

源码位于 `server/`、`web/`、`contracts/`、`tooling/`、`deploy/` 和 `docs/`。缓存、数据库、编译结果、日志、截图、备份及临时文件均在 `.local/`；`node_modules/` 由 npm 管理并忽略。

Docker 沿用宿主代理与认证配置。托管云端识别 `CODEX_PROXY_CERT`；也可设置 `ANIMEMO_BUILD_CA`。构建通过临时 secret 挂载 CA，保留 TLS 校验，最终镜像不包含会话 CA。不要替换全局 Docker 配置或存储驱动。

邮箱验证 / 找回、Bangumi、外部授权与同步、R2 的实现和真实联调边界见 [网络受限交接](docs/local-network-handoff.md)。Linux 或 Windows 上有网络与凭据的环境均可接手，未验证项只阻塞对应能力。插件第一阶段的使用与边界见 [插件说明](docs/plugins.md)；完整第三方隔离、个人授权、市场 / 投稿审核和 Bridge 仍待开发。

其他说明：[云端环境](docs/cloud-development.md) · [模块架构](docs/architecture.md) · [功能对照 JSON](docs/feature-parity.json) · [当前更新路线](docs/refactor-next-steps.md)。
