# 云端开发交接

执行顺序以 [当前更新路线](refactor-next-steps.md) 为准。主开发与首批部署基线为 Linux + Docker；Windows 保留本地接手入口。VM 和约 25 分钟负载测试是可选专项，不作为开发、合并或发布的默认前置。

2026-10-09 接手补充：当前源码和云端 `stage3-review` 为 `1.4.0-alpha.2`，迁移到 023，已加入札记主题与最新默认 UI。GitHub 分支写入本轮成功，镜像、升级中发现的版本冲突及 vfs 空间问题见 [1.4 首批交付](v1.4-delivery.md)。下面的阶段记录保留其历史范围，不用旧环境限制代替当前检查结果。

## 代码位置和范围

本地源代码位于 `animemo-next`，在 [yanyuhanyue/AniMemo](https://github.com/yanyuhanyue/AniMemo/tree/codex/animemo-next) 的 `codex/animemo-next` 分支中位于仓库根目录。当前工作区已继续实现完整手账管理、账号安全、持久导入、分享、专栏、审核、实例安装和备份更新流程；实际验收见 `verification.md`，原项目功能覆盖见 `feature-parity.md`。云端工作区路径为 `/workspace/animemo-next`，工作分支为 `codex/animemo-next-cloud-check`；未推送的改动需要通过源码交接包或后续代码同步取得。

云端环境选择仓库 `yanyuhanyue/AniMemo` 和分支 `codex/animemo-next`，然后在仓库根目录执行下述命令。本地 `.local/` 配置、数据库和 `node_modules/` 不随 Git 同步，云端会创建自己的开发数据。

## 云端环境要求

- Linux。
- Node.js 24.12+、npm。
- Go 1.26.6+，或在环境设置中准备兼容工具链。
- Docker Engine 与 Compose 2.24.4+，可实际启动容器；隔离验收使用 Compose 的 `!override`。
- 包源与镜像源访问权限：npm、Go modules、Docker Hub，以及 Dockerfile 使用的 `gcr.io`。

先检查：

```sh
node --version
npm --version
go version
docker info
docker compose version
```

如果 `docker info` 失败，应先修复云端环境，不能将安装了 Docker CLI 记作容器验证通过。

本次云端的 `/usr/bin/go` 实际是同名围棋程序。Go 1.26.8 已按官方 SHA-256 校验并安装在 `.local/tools/go`；执行 `. ./tooling/env.sh` 后使用这份工具链。重新创建环境时需要重新准备 Go，项目不会自动下载或全局安装工具。

### 托管 vfs 环境的磁盘观察

当前托管 Docker 使用 vfs，构建缓存的实际磁盘占用会明显大于 Docker 显示的逻辑大小。RC2 构建后曾只剩约 611 MiB；清理可回收构建缓存后恢复到约 16 GiB，实例、备份与回滚镜像均保留。`--keep-storage 512MB` 限的是逻辑缓存量，不能保证这个环境的文件系统仍有足够空间。

大构建完成、运行备份/恢复演练之前检查 `df -h .`；空间不足时先核对 `docker system df`，仅按需执行 `docker builder prune -f` 清理未使用构建缓存。不要通过清空实例卷、备份或切换存储驱动解决空间问题。该操作是当前受限环境的维护步骤，不加入日常编译或生产启动流程。

## 从源码开始

所有命令在本项目根目录执行：

```sh
. ./tooling/env.sh
npm ci --ignore-scripts
npm run db:start
npm run dev
```

修改后按路线中的范围表选择检查。需要完整回归时，`npm run verify` 并行检查前后端，再运行 Node 回归与真实 PostgreSQL 的 Go `-race` 集成测试（包含 Go 单元测试）；它不是启动开发或每次提交的前提。Linux race 需要 C 编译器，例如 `cc`；本次环境已具备。重复运行保留编译缓存，但数据库测试始终使用 `-count=1`，不会把上次测试结果当作新结果。

`npm run test:containers` 独立完成镜像构建、页面与真实 API 流程、同时重建应用和数据库后的留存验证，最后清理本次 Compose project 和数据卷。它不依赖开发数据库或本机编译产物，可以直接运行。失败时仍返回非零退出码，结果在 `.local/output/container-smoke.json`；清理失败会额外报告本次 project 名称，不会静默记为通过。

需要保留可访问的开发容器时运行 `npm run containers`，它复用已有镜像构建缓存，并使用开发数据库配置启动应用，不要求先执行容器测试。检查：

```sh
curl --fail http://127.0.0.1:18081/api/ready
curl --fail http://127.0.0.1:18081/
```

在源码开发模式下运行 `npm run dev`。如果云端预览使用外部 HTTPS 域名，把 `PUBLIC_ORIGIN` 设置为准确的预览来源（不带路径），使 Cookie 与写请求来源校验保持一致。Vite 默认只监听回环地址；如环境需要端口代理，按该环境的实际路由方式设置监听地址和允许主机，保持允许范围明确。

## 文件管理

源码按 `server/`、`web/`、`contracts/`、`tooling/`、`deploy/`、`docs/` 分类。工作区内生成的缓存、数据库、日志、截图和编译结果全部落在项目 `.local/`。开发脚本会设置 npm、Go 和临时目录；Docker 沿用已有 `DOCKER_CONFIG` 或宿主默认配置，保留云端的 registry 凭据和代理默认值，不复制认证内容进仓库。隔离验收的数据库是仅本轮使用的 Docker volume，结束时删除；Docker 镜像与构建缓存由 daemon 管理。

## 云端代理与构建空间

托管环境提供 `CODEX_PROXY_CERT` 时，npm 容器入口自动加载 `deploy/compose.proxy.yaml`；也可以显式设置 `ANIMEMO_BUILD_CA`。Node 安装阶段使用 `NODE_EXTRA_CA_CERTS`，Go 下载阶段使用 `SSL_CERT_FILE`，只在对应 RUN 中挂载 CA。Go 编译阶段禁用模块下载，复用已下载模块和编译缓存。不要关闭 TLS 校验，或把 session CA COPY 到最终镜像。

本次 Docker 使用 `vfs`，每层会占用完整文件树，`docker system df` 的逻辑大小不能准确反映物理占用。曾因先前诊断的旧构建缓存和较大的 Go bookworm 构建阶段耗尽 32GB 空间；已清理本任务产生的无用缓存，并改用 Alpine Go 构建阶段，最终运行镜像仍为 distroless。遇到类似问题先检查 `df -h` 与本任务缓存归属，不能直接清空其他项目的镜像或数据卷。

网络仍受云端 allowlist 限制。npm、Go modules、Docker Hub 和 `gcr.io` 已实际访问成功；`cdn.playwright.dev` 下载返回 403。本轮浏览器验证使用系统 Chromium；如果未来需要下载 Playwright 指定版本，应通过云端环境设置增加对应域名，终端无法更改平台访问策略。

## 已有验证与日常推进

已执行 Linux 镜像构建、Compose 启动、`/api/ready`、注册登录、创建番剧与记录观看，并验证容器重建后会话、账号和业务数据仍然存在。本地数据库测试与容器检查的结果分别记录在 [验收记录](verification.md)。

日常修改先运行受影响模块；纯文档检查差异与链接，UI 检查前端及受影响交互，业务和数据变更检查对应真实 PostgreSQL 场景，部署变更补相关容器路径。需要综合回归时再执行 `verify`。这些检查可以在云端开发中直接完成，不用提交后等待 GitHub 才定位问题。GitHub CI 继续承担独立的提交验证，本地结果不会伪造或替代远端状态。可选 VM/长时负载不进入等待链，也不要求每轮完整重跑。

截至本次路线修订，迁移已推进到 `015_extension_identity.sql`。Web 和独立 Go Worker 分开运行，持久任务与 Outbox 存 PostgreSQL；当前不使用 Redis 或额外队列服务。`npm run test:instance` 另行验证安装、实例备份迁移、更新数据库副本、显式回滚以及候选镜像失败时自动恢复。两步验证的 `ANIMEMO_SECRET_KEY` 必须持久保存，开发脚本会生成；独立实例备份会包含原密钥。

1.1 增加 `doctor`、默认只预览的 `configure`、v3 备份清单、恢复计划、age 转移和独立发行入口。实例生命周期验收需先按 [实例操作](instance-operations.md) 安装项目内 age 工具；新增中断路径可用 `ANIMEMO_CANDIDATE_IMAGE=已有镜像 node tooling/recovery-smoke.mjs` 定向验证。正式签发和下载验证受限时按 [发行说明](release.md) 本地接手，不重复构建候选或等待可选专项。

外部服务的已有实现、待真实联调项及本地接手步骤见 [local-network-handoff.md](local-network-handoff.md)。无需等待云端网络放通；继续推进无关能力，再在有网络和凭据的 Linux/Windows 环境中完成对应联调。

参考：[Codex Cloud 环境说明](https://learn.chatgpt.com/docs/environments/cloud-environments)。

## 2026-10-07 阶段 1–3 接手

已同步 `4eb8b88`，本轮工作仍在 `codex/animemo-next-cloud-check`；旧云端改动的安全 stash 保留，没有覆盖本地推送。源码与运行结果见 [阶段交付](stage-1-3-delivery.md) 和 [验证](verification.md)。

当前云端验收实例：`stage3-review`，回环端口 `18082`；app / worker / db 分开，均有健康检查，使用专用数据卷。合成验收账号保存在 `.local/output/stage3-review-access.json`，该文件不进入 Git 或源码交接包。停止/恢复：

```sh
npm run instance -- stop --name stage3-review
npm run instance -- start --name stage3-review
npm run instance -- status --name stage3-review
```

当前环境只开放依赖下载网络，未配置 VPN、远端服务凭据或用户侧端口转发。云端 `127.0.0.1:18082` 不能直接当作 Windows 本地地址。若没有已有转发，接收当前源码后在本地执行：

```powershell
. ./tooling/env.ps1
npm ci --ignore-scripts
npm run db:start
npm run dev
```

打开本地 `http://127.0.0.1:5177`。三个进程（Web、Worker、Vite）由脚本统一启动和停止。也可以 `npm run containers` 后访问本地 `http://127.0.0.1:18081`；正式实例、首次管理员初始化和外部服务配置按原有章节执行。

本轮 vfs 缓存再次积累，占满风险已通过清理本任务旧构建缓存解除；未删除原开发库、原镜像、源码或安全 stash。恢复验收同时修复了 Linux 私有媒体备份文件读取权限和 Compose profile 导致 Worker 清理遗漏的问题，不需要等待 GitHub CI 才发现。
