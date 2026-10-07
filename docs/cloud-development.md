# 云端开发交接

## 代码位置和范围

本地源代码位于 `animemo-next`，在 [yanyuhanyue/AniMemo](https://github.com/yanyuhanyue/AniMemo/tree/codex/animemo-next) 的 `codex/animemo-next` 分支中位于仓库根目录。当前工作区已继续实现完整手账管理、账号安全、持久导入、分享、专栏、审核、实例安装和备份更新流程；最新验收和未完成项以 `feature-parity.md` 为准。云端工作区路径为 `/workspace/animemo-next`，工作分支为 `codex/animemo-next-cloud-check`；未推送的改动需要通过源码交接包或后续代码同步取得。

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

## 从源码开始

所有命令在本项目根目录执行：

```sh
. ./tooling/env.sh
npm ci --ignore-scripts
npm run db:start
npm run verify
npm run test:containers
```

`npm run verify` 并行检查前后端，然后运行 Node 回归与真实 PostgreSQL 的 Go `-race` 集成测试（包含 Go 单元测试）。Linux 需要 C 编译器，例如 `cc`；本次环境已具备。重复运行保留编译缓存，但数据库测试始终使用 `-count=1`，不会把上次测试结果当作新结果。

`npm run test:containers` 独立完成镜像构建、页面与真实 API 流程、同时重建应用和数据库后的留存验证，最后清理本次 Compose project 和数据卷。它不依赖开发数据库或本机编译产物，可以直接运行。失败时仍返回非零退出码，结果在 `.local/output/container-smoke.json`；清理失败会额外报告本次 project 名称，不会静默记为通过。

需要保留可访问的开发容器时运行 `npm run containers`，它复用前一步的镜像构建缓存，并使用开发数据库配置启动应用。检查：

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

## 云端接手后的第一项工作

已执行 Linux 镜像构建、Compose 启动、`/api/ready`、注册登录、创建番剧与记录观看，并验证容器重建后会话、账号和业务数据仍然存在。本地数据库测试与容器检查的结果分别记录在 [验收记录](verification.md)。

日常修改先运行受影响模块；提交前用 `verify`，涉及应用集成、依赖或部署时补 `test:containers`。这些检查可以在云端开发中直接完成，不用提交后等待 GitHub 才定位问题。GitHub CI 继续承担独立的提交验证，本地结果不会伪造或替代远端状态。

当前迁移已推进到 `007_public_query_indexes.sql`。应用内有持久导入 worker；不需要另设队列服务。`npm run test:instance` 另行验证安装、实例备份迁移、更新数据库副本、显式回滚以及候选镜像失败时自动恢复。两步验证的 `ANIMEMO_SECRET_KEY` 必须持久保存，开发脚本会生成；独立实例备份会包含原密钥。

网络受限的未实现业务和 Windows 本地接手步骤见 [local-network-handoff.md](local-network-handoff.md)。无需继续等待云端网络；先完成可验证的本地能力，再按该文档接通外部服务。

参考：[Codex Cloud 环境说明](https://learn.chatgpt.com/docs/environments/cloud-environments)。
